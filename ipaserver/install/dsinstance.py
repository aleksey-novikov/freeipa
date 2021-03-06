# Authors: Karl MacMillan <kmacmillan@mentalrootkit.com>
#          Simo Sorce <ssorce@redhat.com>
#
# Copyright (C) 2007  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from __future__ import print_function

import shutil
import pwd
import os
import re
import time
import tempfile
import stat
import fnmatch

import ldap

from ipalib.install import certmonger, certstore
from ipapython.ipa_log_manager import root_logger
from ipapython import ipautil, ipaldap
from ipapython import dogtag
from ipaserver.install import service
from ipaserver.install import installutils
from ipaserver.install import certs
from ipaserver.install import ldapupdate
from ipaserver.install import replication
from ipaserver.install import sysupgrade
from ipaserver.install import upgradeinstance
from ipalib import api
from ipalib import errors
from ipalib import constants
from ipaplatform.constants import constants as platformconstants
from ipaplatform.tasks import tasks
from ipapython.dn import DN
from ipapython.admintool import ScriptError
from ipaplatform import services
from ipaplatform.paths import paths

DS_USER = platformconstants.DS_USER
DS_GROUP = platformconstants.DS_GROUP

IPA_SCHEMA_FILES = ("60kerberos.ldif",
                    "60samba.ldif",
                    "60ipaconfig.ldif",
                    "60basev2.ldif",
                    "60basev3.ldif",
                    "60ipapk11.ldif",
                    "60ipadns.ldif",
                    "60certificate-profiles.ldif",
                    "61kerberos-ipav3.ldif",
                    "65ipacertstore.ldif",
                    "65ipasudo.ldif",
                    "70ipaotp.ldif",
                    "70topology.ldif",
                    "71idviews.ldif",
                    "72domainlevels.ldif",
                    "15rfc2307bis.ldif",
                    "15rfc4876.ldif")

ALL_SCHEMA_FILES = IPA_SCHEMA_FILES + ("05rfc2247.ldif", )
DS_INSTANCE_PREFIX = 'slapd-'


def find_server_root():
    if ipautil.dir_exists(paths.USR_LIB_DIRSRV_64):
        return paths.USR_LIB_DIRSRV_64
    else:
        return paths.USR_LIB_DIRSRV

def config_dirname(serverid):
    return (paths.ETC_DIRSRV_SLAPD_INSTANCE_TEMPLATE % serverid) + "/"

def schema_dirname(serverid):
    return config_dirname(serverid) + "/schema/"


def remove_ds_instance(serverid, force=False):
    """A wrapper around the 'remove-ds.pl' script used by
    389ds to remove a single directory server instance. In case of error
    additional call with the '-f' flag is performed (forced removal). If this
    also fails, then an exception is raised.
    """
    instance_name = ''.join([DS_INSTANCE_PREFIX, serverid])
    args = [paths.REMOVE_DS_PL, '-i', instance_name]
    if force:
        args.append('-f')
        root_logger.debug("Forcing instance removal")

    try:
        ipautil.run(args)
    except ipautil.CalledProcessError:
        if force:
            root_logger.error("Instance removal failed.")
            raise
        root_logger.debug("'%s' failed. "
                          "Attempting to force removal" % paths.REMOVE_DS_PL)
        remove_ds_instance(serverid, force=True)


def get_ds_instances():
    '''
    Return a sorted list of all 389ds instances.

    If the instance name ends with '.removed' it is ignored. This
    matches 389ds behavior.
    '''

    dirsrv_instance_dir = paths.ETC_DIRSRV

    instances = []

    for basename in os.listdir(dirsrv_instance_dir):
        pathname = os.path.join(dirsrv_instance_dir, basename)
        # Must be a directory
        if os.path.isdir(pathname):
            # Must start with prefix and not end with .removed
            if (basename.startswith(DS_INSTANCE_PREFIX) and
                    not basename.endswith('.removed')):
                # Strip off prefix
                instance = basename[len(DS_INSTANCE_PREFIX):]
                # Must be non-empty
                if instance:
                    instances.append(instance)

    instances.sort()
    return instances

def check_ports():
    """
    Check of Directory server ports are open.

    Returns a tuple with two booleans, one for unsecure port 389 and one for
    secure port 636. True means that the port is free, False means that the
    port is taken.
    """
    ds_unsecure = not ipautil.host_port_open(None, 389)
    ds_secure = not ipautil.host_port_open(None, 636)
    return (ds_unsecure, ds_secure)

def is_ds_running(server_id=''):
    return services.knownservices.dirsrv.is_running(instance_name=server_id)


def create_ds_user():
    """Create DS user/group if it doesn't exist yet."""
    tasks.create_system_user(
        name=DS_USER,
        group=DS_USER,
        homedir=paths.VAR_LIB_DIRSRV,
        shell=paths.NOLOGIN,
    )


def get_domain_level(api=api):
    ldap_uri = ipaldap.get_ldap_uri(protocol='ldapi', realm=api.env.realm)
    conn = ipaldap.LDAPClient(ldap_uri)
    conn.external_bind()

    dn = DN(('cn', 'Domain Level'),
            ('cn', 'ipa'), ('cn', 'etc'), api.env.basedn)

    try:
        entry = conn.get_entry(dn, ['ipaDomainLevel'])
    except errors.NotFound:
        return constants.DOMAIN_LEVEL_0
    return int(entry.single_value['ipaDomainLevel'])


def get_all_external_schema_files(root):
    """Get all schema files"""
    f = []
    for path, _subdirs, files in os.walk(root):
        for name in files:
            if fnmatch.fnmatch(name, "*.ldif"):
                f.append(os.path.join(path, name))
    return f


INF_TEMPLATE = """
[General]
FullMachineName=   $FQDN
SuiteSpotUserID=   $USER
SuiteSpotGroup=    $GROUP
ServerRoot=    $SERVER_ROOT
[slapd]
ServerPort=   389
ServerIdentifier=   $SERVERID
Suffix=   $SUFFIX
RootDN=   cn=Directory Manager
RootDNPwd= $PASSWORD
InstallLdifFile= /var/lib/dirsrv/boot.ldif
inst_dir=   /var/lib/dirsrv/scripts-$SERVERID
"""

BASE_TEMPLATE = """
dn: $SUFFIX
objectClass: top
objectClass: domain
objectClass: pilotObject
dc: $BASEDC
info: IPA V2.0
"""


class DsInstance(service.Service):
    def __init__(self, realm_name=None, domain_name=None, fstore=None,
                 domainlevel=None, config_ldif=None):
        super(DsInstance, self).__init__(
            "dirsrv",
            service_desc="directory server",
            fstore=fstore,
            service_prefix=u'ldap',
            keytab=paths.DS_KEYTAB,
            service_user=DS_USER,
            realm_name=realm_name
        )
        self.nickname = 'Server-Cert'
        self.sub_dict = None
        self.domain = domain_name
        self.serverid = None
        self.master_fqdn = None
        self.pkcs12_info = None
        self.cacert_name = None
        self.ca_is_configured = True
        self.dercert = None
        self.idstart = None
        self.idmax = None
        self.ca_subject = None
        self.subject_base = None
        self.open_ports = []
        self.run_init_memberof = True
        self.config_ldif = config_ldif  # updates for dse.ldif
        self.domainlevel = domainlevel
        if realm_name:
            self.suffix = ipautil.realm_to_suffix(self.realm)
            self.__setup_sub_dict()
        else:
            self.suffix = DN()

    subject_base = ipautil.dn_attribute_property('_subject_base')

    def __common_setup(self, enable_ssl=False):

        self.step("creating directory server user", create_ds_user)
        self.step("creating directory server instance", self.__create_instance)
        self.step("enabling ldapi", self.__enable_ldapi)
        self.step("configure autobind for root", self.__root_autobind)
        self.step("stopping directory server", self.__stop_instance)
        self.step("updating configuration in dse.ldif", self.__update_dse_ldif)
        self.step("starting directory server", self.__start_instance)
        self.step("adding default schema", self.__add_default_schemas)
        self.step("enabling memberof plugin", self.__add_memberof_module)
        self.step("enabling winsync plugin", self.__add_winsync_module)
        self.step("configuring replication version plugin", self.__config_version_module)
        self.step("enabling IPA enrollment plugin", self.__add_enrollment_module)
        self.step("configuring uniqueness plugin", self.__set_unique_attrs)
        self.step("configuring uuid plugin", self.__config_uuid_module)
        self.step("configuring modrdn plugin", self.__config_modrdn_module)
        self.step("configuring DNS plugin", self.__config_dns_module)
        self.step("enabling entryUSN plugin", self.__enable_entryusn)
        self.step("configuring lockout plugin", self.__config_lockout_module)
        self.step("configuring topology plugin", self.__config_topology_module)
        self.step("creating indices", self.__create_indices)
        self.step("enabling referential integrity plugin", self.__add_referint_module)
        if enable_ssl:
            self.step("configuring TLS for DS instance", self.__enable_ssl)
        self.step("configuring certmap.conf", self.__certmap_conf)
        self.step("configure new location for managed entries", self.__repoint_managed_entries)
        self.step("configure dirsrv ccache", self.configure_dirsrv_ccache)
        self.step("enabling SASL mapping fallback",
                  self.__enable_sasl_mapping_fallback)

    def __common_post_setup(self):
        self.step("initializing group membership", self.init_memberof)
        self.step("adding master entry", self.__add_master_entry)
        self.step("initializing domain level", self.__set_domain_level)
        self.step("configuring Posix uid/gid generation",
                  self.__config_uidgid_gen)
        self.step("adding replication acis", self.__add_replication_acis)
        self.step("enabling compatibility plugin",
                  self.__enable_compat_plugin)
        self.step("activating sidgen plugin", self._add_sidgen_plugin)
        self.step("activating extdom plugin", self._add_extdom_plugin)
        self.step("tuning directory server", self.__tuning)

        self.step("configuring directory to start on boot", self.__enable)

    def init_info(self, realm_name, fqdn, domain_name, dm_password,
                  subject_base, ca_subject,
                  idstart, idmax, pkcs12_info, ca_file=None):
        self.realm = realm_name.upper()
        self.serverid = installutils.realm_to_serverid(self.realm)
        self.suffix = ipautil.realm_to_suffix(self.realm)
        self.fqdn = fqdn
        self.dm_password = dm_password
        self.domain = domain_name
        self.subject_base = subject_base
        self.ca_subject = ca_subject
        self.idstart = idstart
        self.idmax = idmax
        self.pkcs12_info = pkcs12_info
        if pkcs12_info:
            self.ca_is_configured = False
        self.ca_file = ca_file

        self.__setup_sub_dict()

    def create_instance(self, realm_name, fqdn, domain_name,
                        dm_password, pkcs12_info=None,
                        idstart=1100, idmax=999999,
                        subject_base=None, ca_subject=None,
                        hbac_allow=True, ca_file=None):
        self.init_info(
            realm_name, fqdn, domain_name, dm_password,
            subject_base, ca_subject,
            idstart, idmax, pkcs12_info, ca_file=ca_file)

        self.__common_setup()
        self.step("restarting directory server", self.__restart_instance)

        self.step("adding sasl mappings to the directory", self.__configure_sasl_mappings)
        self.step("adding default layout", self.__add_default_layout)
        self.step("adding delegation layout", self.__add_delegation_layout)
        self.step("creating container for managed entries", self.__managed_entries)
        self.step("configuring user private groups", self.__user_private_groups)
        self.step("configuring netgroups from hostgroups", self.__host_nis_groups)
        self.step("creating default Sudo bind user", self.__add_sudo_binduser)
        self.step("creating default Auto Member layout", self.__add_automember_config)
        self.step("adding range check plugin", self.__add_range_check_plugin)
        if hbac_allow:
            self.step("creating default HBAC rule allow_all", self.add_hbac)
        self.step("adding entries for topology management", self.__add_topology_entries)

        self.__common_post_setup()

        self.start_creation(runtime=60)

    def enable_ssl(self):
        self.steps = []

        self.step("configuring TLS for DS instance", self.__enable_ssl)
        self.step("restarting directory server", self.__restart_instance)
        self.step("adding CA certificate entry", self.__upload_ca_cert)

        self.start_creation(runtime=10)

    def create_replica(self, realm_name, master_fqdn, fqdn,
                       domain_name, dm_password,
                       subject_base, ca_subject,
                       api, pkcs12_info=None, ca_file=None,
                       ca_is_configured=None, promote=False):
        # idstart and idmax are configured so that the range is seen as
        # depleted by the DNA plugin and the replica will go and get a
        # new range from the master.
        # This way all servers use the initially defined range by default.
        idstart = 1101
        idmax = 1100

        self.init_info(
            realm_name=realm_name,
            fqdn=fqdn,
            domain_name=domain_name,
            dm_password=dm_password,
            subject_base=subject_base,
            ca_subject=ca_subject,
            idstart=idstart,
            idmax=idmax,
            pkcs12_info=pkcs12_info,
            ca_file=ca_file
        )
        self.master_fqdn = master_fqdn
        if ca_is_configured is not None:
            self.ca_is_configured = ca_is_configured
        self.promote = promote
        self.api = api

        self.__common_setup(enable_ssl=(not self.promote))
        self.step("restarting directory server", self.__restart_instance)

        self.step("creating DS keytab", self._request_service_keytab)
        if self.promote:
            if self.pkcs12_info:
                self.step("configuring TLS for DS instance", self.__enable_ssl)
            else:
                self.step("retrieving DS Certificate", self.__get_ds_cert)
            self.step("restarting directory server", self.__restart_instance)

        self.step("setting up initial replication", self.__setup_replica)
        self.step("adding sasl mappings to the directory", self.__configure_sasl_mappings)
        self.step("updating schema", self.__update_schema)
        # See LDIFs for automember configuration during replica install
        self.step("setting Auto Member configuration", self.__add_replica_automember_config)
        self.step("enabling S4U2Proxy delegation", self.__setup_s4u2proxy)
        self.step("importing CA certificates from LDAP", self.__import_ca_certs)

        self.__common_post_setup()

        self.start_creation(runtime=60)


    def __setup_replica(self):
        """
        Setup initial replication between replica and remote master.
        GSSAPI is always used as a replication bind method. Note, however,
        that the bind method for the replication differs between domain levels:
            * in domain level 0, Directory Manager credentials are used to bind
              to remote master
            * in domain level 1, GSSAPI using admin/privileged host credentials
              is used (we do not have access to masters' DM password in this
              stage)
        """
        replication.enable_replication_version_checking(
            self.realm,
            self.dm_password)

        # Always connect to self over ldapi
        ldap_uri = ipaldap.get_ldap_uri(protocol='ldapi', realm=self.realm)
        conn = ipaldap.LDAPClient(ldap_uri)
        conn.external_bind()
        repl = replication.ReplicationManager(self.realm,
                                              self.fqdn,
                                              self.dm_password, conn=conn)

        if self.dm_password is not None and not self.promote:
            bind_dn = DN(('cn', 'Directory Manager'))
            bind_pw = self.dm_password
        else:
            bind_dn = bind_pw = None

        repl.setup_promote_replication(self.master_fqdn,
                                       r_binddn=bind_dn,
                                       r_bindpw=bind_pw,
                                       cacert=self.ca_file)
        self.run_init_memberof = repl.needs_memberof_fixup()

    def __configure_sasl_mappings(self):
        # we need to remove any existing SASL mappings in the directory as otherwise they
        # they may conflict.

        try:
            res = api.Backend.ldap2.get_entries(
                DN(('cn', 'mapping'), ('cn', 'sasl'), ('cn', 'config')),
                api.Backend.ldap2.SCOPE_ONELEVEL,
                "(objectclass=nsSaslMapping)")
            for r in res:
                try:
                    api.Backend.ldap2.delete_entry(r)
                except Exception as e:
                    root_logger.critical(
                        "Error during SASL mapping removal: %s", e)
                    raise
        except Exception as e:
            root_logger.critical("Error while enumerating SASL mappings %s", e)
            raise

        entry = api.Backend.ldap2.make_entry(
            DN(
                ('cn', 'Full Principal'), ('cn', 'mapping'), ('cn', 'sasl'),
                ('cn', 'config')),
            objectclass=["top", "nsSaslMapping"],
            cn=["Full Principal"],
            nsSaslMapRegexString=['\(.*\)@\(.*\)'],
            nsSaslMapBaseDNTemplate=[self.suffix],
            nsSaslMapFilterTemplate=['(krbPrincipalName=\\1@\\2)'],
            nsSaslMapPriority=['10'],
        )
        api.Backend.ldap2.add_entry(entry)

        entry = api.Backend.ldap2.make_entry(
            DN(
                ('cn', 'Name Only'), ('cn', 'mapping'), ('cn', 'sasl'),
                ('cn', 'config')),
            objectclass=["top", "nsSaslMapping"],
            cn=["Name Only"],
            nsSaslMapRegexString=['^[^:@]+$'],
            nsSaslMapBaseDNTemplate=[self.suffix],
            nsSaslMapFilterTemplate=['(krbPrincipalName=&@%s)' % self.realm],
            nsSaslMapPriority=['10'],
        )
        api.Backend.ldap2.add_entry(entry)

    def __update_schema(self):
        # FIXME: https://fedorahosted.org/389/ticket/47490
        self._ldap_mod("schema-update.ldif")

    def __enable(self):
        self.backup_state("enabled", self.is_enabled())
        # At the end of the installation ipa-server-install will enable the
        # 'ipa' service wich takes care of starting/stopping dirsrv
        self.disable()

    def __setup_sub_dict(self):
        server_root = find_server_root()
        try:
            idrange_size = self.idmax - self.idstart + 1
        except TypeError:
            idrange_size = None
        self.sub_dict = dict(FQDN=self.fqdn, SERVERID=self.serverid,
                             PASSWORD=self.dm_password,
                             RANDOM_PASSWORD=ipautil.ipa_generate_password(),
                             SUFFIX=self.suffix,
                             REALM=self.realm, USER=DS_USER,
                             SERVER_ROOT=server_root, DOMAIN=self.domain,
                             TIME=int(time.time()), IDSTART=self.idstart,
                             IDMAX=self.idmax, HOST=self.fqdn,
                             ESCAPED_SUFFIX=str(self.suffix),
                             GROUP=DS_GROUP,
                             IDRANGE_SIZE=idrange_size,
                             DOMAIN_LEVEL=self.domainlevel,
                             MAX_DOMAIN_LEVEL=constants.MAX_DOMAIN_LEVEL,
                             MIN_DOMAIN_LEVEL=constants.MIN_DOMAIN_LEVEL,
                             STRIP_ATTRS=" ".join(replication.STRIP_ATTRS),
                             EXCLUDES='(objectclass=*) $ EXCLUDE ' +
                             ' '.join(replication.EXCLUDES),
                             TOTAL_EXCLUDES='(objectclass=*) $ EXCLUDE ' +
                             ' '.join(replication.TOTAL_EXCLUDES),
                         )

    def __create_instance(self):
        pent = pwd.getpwnam(DS_USER)

        self.backup_state("serverid", self.serverid)
        self.fstore.backup_file(paths.SYSCONFIG_DIRSRV)

        self.sub_dict['BASEDC'] = self.realm.split('.')[0].lower()
        base_txt = ipautil.template_str(BASE_TEMPLATE, self.sub_dict)
        root_logger.debug(base_txt)

        target_fname = paths.DIRSRV_BOOT_LDIF
        base_fd = open(target_fname, "w")
        base_fd.write(base_txt)
        base_fd.close()

        # Must be readable for dirsrv
        os.chmod(target_fname, 0o440)
        os.chown(target_fname, pent.pw_uid, pent.pw_gid)

        inf_txt = ipautil.template_str(INF_TEMPLATE, self.sub_dict)
        root_logger.debug("writing inf template")
        inf_fd = ipautil.write_tmp_file(inf_txt)
        inf_txt = re.sub(r"RootDNPwd=.*\n", "", inf_txt)
        root_logger.debug(inf_txt)
        args = [
            paths.SETUP_DS_PL, "--silent",
            "--logfile", "-",
            "-f", inf_fd.name,
        ]
        root_logger.debug("calling setup-ds.pl")
        try:
            ipautil.run(args)
            root_logger.debug("completed creating DS instance")
        except ipautil.CalledProcessError as e:
            raise RuntimeError("failed to create DS instance %s" % e)

        # check for open port 389 from now on
        self.open_ports.append(389)

        inf_fd.close()
        os.remove(paths.DIRSRV_BOOT_LDIF)

    def __update_dse_ldif(self):
        """
        This method updates dse.ldif right after instance creation. This is
        supposed to allow admin modify configuration of the DS which has to be
        done before IPA is fully installed (for example: settings for
        replication on replicas)
        DS must be turned off.
        """
        dse_filename = os.path.join(
            paths.ETC_DIRSRV_SLAPD_INSTANCE_TEMPLATE % self.serverid,
            'dse.ldif'
        )

        with tempfile.NamedTemporaryFile(delete=False) as new_dse_ldif:
            temp_filename = new_dse_ldif.name
            with open(dse_filename, "r") as input_file:
                parser = installutils.ModifyLDIF(input_file, new_dse_ldif)
                parser.replace_value(
                        'cn=config,cn=ldbm database,cn=plugins,cn=config',
                        'nsslapd-db-locks',
                        ['50000']
                        )
                if self.config_ldif:
                    # parse modifications from ldif file supplied by the admin
                    with open(self.config_ldif, "r") as config_ldif:
                        parser.modifications_from_ldif(config_ldif)
                parser.parse()
            new_dse_ldif.flush()
        shutil.copy2(temp_filename, dse_filename)
        try:
            os.remove(temp_filename)
        except OSError as e:
            root_logger.debug("Failed to clean temporary file: %s" % e)

    def __add_default_schemas(self):
        pent = pwd.getpwnam(DS_USER)
        for schema_fname in IPA_SCHEMA_FILES:
            target_fname = schema_dirname(self.serverid) + schema_fname
            shutil.copyfile(
                os.path.join(paths.USR_SHARE_IPA_DIR, schema_fname),
                target_fname)
            os.chmod(target_fname, 0o440)    # read access for dirsrv user/group
            os.chown(target_fname, pent.pw_uid, pent.pw_gid)

        try:
            shutil.move(schema_dirname(self.serverid) + "05rfc2247.ldif",
                            schema_dirname(self.serverid) + "05rfc2247.ldif.old")

            target_fname = schema_dirname(self.serverid) + "05rfc2247.ldif"
            shutil.copyfile(
                os.path.join(paths.USR_SHARE_IPA_DIR, "05rfc2247.ldif"),
                target_fname)
            os.chmod(target_fname, 0o440)
            os.chown(target_fname, pent.pw_uid, pent.pw_gid)
        except IOError:
            # Does not apply with newer DS releases
            pass

    def start(self, *args, **kwargs):
        super(DsInstance, self).start(*args, **kwargs)
        api.Backend.ldap2.connect()

    def stop(self, *args, **kwargs):
        if api.Backend.ldap2.isconnected():
            api.Backend.ldap2.disconnect()

        super(DsInstance, self).stop(*args, **kwargs)

    def restart(self, instance=''):
        api.Backend.ldap2.disconnect()
        try:
            super(DsInstance, self).restart(instance)
            if not is_ds_running(instance):
                root_logger.critical("Failed to restart the directory server. See the installation log for details.")
                raise ScriptError()
        except SystemExit as e:
            raise e
        except Exception as e:
            # TODO: roll back here?
            root_logger.critical("Failed to restart the directory server (%s). See the installation log for details." % e)
        api.Backend.ldap2.connect()

    def __start_instance(self):
        self.start(self.serverid)

    def __stop_instance(self):
        self.stop(self.serverid)

    def __restart_instance(self):
        self.restart(self.serverid)

    def __enable_entryusn(self):
        self._ldap_mod("entryusn.ldif")

    def __add_memberof_module(self):
        self._ldap_mod("memberof-conf.ldif")

    def init_memberof(self):

        if not self.run_init_memberof:
            return

        self._ldap_mod("memberof-task.ldif", self.sub_dict)
        # Note, keep dn in sync with dn in install/share/memberof-task.ldif
        dn = DN(('cn', 'IPA install %s' % self.sub_dict["TIME"]), ('cn', 'memberof task'),
                ('cn', 'tasks'), ('cn', 'config'))
        root_logger.debug("Waiting for memberof task to complete.")
        ldap_uri = ipaldap.get_ldap_uri(self.fqdn)
        conn = ipaldap.LDAPClient(ldap_uri)
        if self.dm_password:
            conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                             bind_password=self.dm_password)
        else:
            conn.gssapi_bind()
        replication.wait_for_task(conn, dn)
        conn.unbind()

    def apply_updates(self):
        schema_files = get_all_external_schema_files(paths.EXTERNAL_SCHEMA_DIR)
        data_upgrade = upgradeinstance.IPAUpgrade(self.realm,
                                                  schema_files=schema_files)
        try:
            data_upgrade.create_instance()
        except Exception as e:
            # very fatal errors only will raise exception
            raise RuntimeError("Update failed: %s" % e)
        installutils.store_version()


    def __add_referint_module(self):
        self._ldap_mod("referint-conf.ldif")

    def __set_unique_attrs(self):
        self._ldap_mod("unique-attributes.ldif", self.sub_dict)

    def __config_uidgid_gen(self):
        self._ldap_mod("dna.ldif", self.sub_dict)

    def __add_master_entry(self):
        self._ldap_mod("master-entry.ldif", self.sub_dict)

    def __add_topology_entries(self):
        self._ldap_mod("topology-entries.ldif", self.sub_dict)

    def __add_winsync_module(self):
        self._ldap_mod("ipa-winsync-conf.ldif")

    def __enable_compat_plugin(self):
        ld = ldapupdate.LDAPUpdate(dm_password=self.dm_password, sub_dict=self.sub_dict)
        rv = ld.update([paths.SCHEMA_COMPAT_ULDIF])
        if not rv:
            raise RuntimeError("Enabling compatibility plugin failed")

    def __config_version_module(self):
        self._ldap_mod("version-conf.ldif")

    def __config_uuid_module(self):
        self._ldap_mod("uuid-conf.ldif")
        self._ldap_mod("uuid.ldif", self.sub_dict)

    def __config_modrdn_module(self):
        self._ldap_mod("modrdn-conf.ldif")
        self._ldap_mod("modrdn-krbprinc.ldif", self.sub_dict)

    def __config_dns_module(self):
        # Configure DNS plugin unconditionally as we would otherwise have
        # troubles if other replica just configured DNS with ipa-dns-install
        self._ldap_mod("ipa-dns-conf.ldif")

    def __config_lockout_module(self):
        self._ldap_mod("lockout-conf.ldif")

    def __config_topology_module(self):
        self._ldap_mod("ipa-topology-conf.ldif", self.sub_dict)

    def __repoint_managed_entries(self):
        self._ldap_mod("repoint-managed-entries.ldif", self.sub_dict)

    def configure_dirsrv_ccache(self):
        pent = pwd.getpwnam(platformconstants.DS_USER)
        ccache = paths.TMP_KRB5CC % pent.pw_uid
        filepath = paths.SYSCONFIG_DIRSRV
        if not os.path.exists(filepath):
            # file doesn't exist; create it with correct ownership & mode
            open(filepath, 'a').close()
            os.chmod(filepath,
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            os.chown(filepath, 0, 0)

        replacevars = {'KRB5CCNAME': ccache}
        ipautil.backup_config_and_replace_variables(
            self.fstore, filepath, replacevars=replacevars)
        tasks.restore_context(filepath)

    def __managed_entries(self):
        self._ldap_mod("managed-entries.ldif", self.sub_dict)

    def __user_private_groups(self):
        self._ldap_mod("user_private_groups.ldif", self.sub_dict)

    def __host_nis_groups(self):
        self._ldap_mod("host_nis_groups.ldif", self.sub_dict)

    def __add_enrollment_module(self):
        self._ldap_mod("enrollment-conf.ldif", self.sub_dict)

    def __enable_ssl(self):
        dirname = config_dirname(self.serverid)
        dsdb = certs.CertDB(
            self.realm,
            nssdir=dirname,
            subject_base=self.subject_base,
            ca_subject=self.ca_subject,
        )
        if self.pkcs12_info:
            if self.ca_is_configured:
                trust_flags = 'CT,C,C'
            else:
                trust_flags = None
            dsdb.create_from_pkcs12(self.pkcs12_info[0], self.pkcs12_info[1],
                                    ca_file=self.ca_file,
                                    trust_flags=trust_flags)
            server_certs = dsdb.find_server_certs()
            if len(server_certs) == 0:
                raise RuntimeError("Could not find a suitable server cert in import in %s" % self.pkcs12_info[0])

            # We only handle one server cert
            self.nickname = server_certs[0][0]
            self.dercert = dsdb.get_cert_from_db(self.nickname, pem=False)

            if self.ca_is_configured:
                dsdb.track_server_cert(
                    self.nickname, self.principal, dsdb.passwd_fname,
                    'restart_dirsrv %s' % self.serverid)
        else:
            cadb = certs.CertDB(self.realm, host_name=self.fqdn, subject_base=self.subject_base)

            # FIXME, need to set this nickname in the RA plugin
            cadb.export_ca_cert('ipaCert', False)
            dsdb.create_from_cacert(cadb.cacert_fname, passwd=None)
            ca_args = ['/usr/libexec/certmonger/dogtag-submit',
                       '--ee-url', 'https://%s:8443/ca/ee/ca' % self.fqdn,
                       '--dbdir', paths.HTTPD_ALIAS_DIR,
                       '--nickname', 'ipaCert',
                       '--sslpinfile', paths.ALIAS_PWDFILE_TXT,
                       '--agent-submit']
            helper = " ".join(ca_args)
            prev_helper = certmonger.modify_ca_helper('IPA', helper)
            try:
                cmd = 'restart_dirsrv %s' % self.serverid
                certmonger.request_and_wait_for_cert(
                    certpath=dirname,
                    nickname=self.nickname,
                    principal=self.principal,
                    passwd_fname=dsdb.passwd_fname,
                    subject=str(DN(('CN', self.fqdn), self.subject_base)),
                    ca='IPA',
                    profile=dogtag.DEFAULT_PROFILE,
                    dns=[self.fqdn],
                    post_command=cmd)
            finally:
                certmonger.modify_ca_helper('IPA', prev_helper)

            self.dercert = dsdb.get_cert_from_db(self.nickname, pem=False)
            dsdb.create_pin_file()

        self.cacert_name = dsdb.cacert_name

        ldap_uri = ipaldap.get_ldap_uri(self.fqdn)
        conn = ipaldap.LDAPClient(ldap_uri)
        conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                         bind_password=self.dm_password)

        mod = [(ldap.MOD_REPLACE, "nsSSLClientAuth", "allowed"),
               (ldap.MOD_REPLACE, "nsSSL3Ciphers", "default"),
               (ldap.MOD_REPLACE, "allowWeakCipher", "off")]
        conn.modify_s(DN(('cn', 'encryption'), ('cn', 'config')), mod)

        mod = [(ldap.MOD_ADD, "nsslapd-security", "on")]
        conn.modify_s(DN(('cn', 'config')), mod)

        entry = conn.make_entry(
            DN(('cn', 'RSA'), ('cn', 'encryption'), ('cn', 'config')),
            objectclass=["top", "nsEncryptionModule"],
            cn=["RSA"],
            nsSSLPersonalitySSL=[self.nickname],
            nsSSLToken=["internal (software)"],
            nsSSLActivation=["on"],
        )
        conn.add_entry(entry)

        conn.unbind()

        # check for open secure port 636 from now on
        self.open_ports.append(636)

    def __upload_ca_cert(self):
        """
        Upload the CA certificate from the NSS database to the LDAP directory.
        """

        dirname = config_dirname(self.serverid)
        dsdb = certs.CertDB(self.realm, nssdir=dirname,
                            subject_base=self.subject_base)
        trust_flags = dict(reversed(dsdb.list_certs()))

        ldap_uri = ipaldap.get_ldap_uri(self.fqdn)
        conn = ipaldap.LDAPClient(ldap_uri)
        conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                         bind_password=self.dm_password)

        nicknames = dsdb.find_root_cert(self.cacert_name)[:-1]
        for nickname in nicknames:
            cert = dsdb.get_cert_from_db(nickname, pem=False)
            certstore.put_ca_cert_nss(conn, self.suffix, cert, nickname,
                                      trust_flags[nickname])

        nickname = self.cacert_name
        cert = dsdb.get_cert_from_db(nickname, pem=False)
        certstore.put_ca_cert_nss(conn, self.suffix, cert, nickname,
                                  trust_flags[nickname],
                                  config_ipa=self.ca_is_configured,
                                  config_compat=self.master_fqdn is None)

        conn.unbind()

    def __import_ca_certs(self):
        dirname = config_dirname(self.serverid)
        dsdb = certs.CertDB(self.realm, nssdir=dirname,
                            subject_base=self.subject_base)

        ldap_uri = ipaldap.get_ldap_uri(self.fqdn)
        conn = ipaldap.LDAPClient(ldap_uri)
        conn.simple_bind(bind_dn=ipaldap.DIRMAN_DN,
                         bind_password=self.dm_password)

        self.import_ca_certs(dsdb, self.ca_is_configured, conn)

        conn.unbind()

    def __add_default_layout(self):
        self._ldap_mod("bootstrap-template.ldif", self.sub_dict)

    def __add_delegation_layout(self):
        self._ldap_mod("delegation.ldif", self.sub_dict)

    def __add_replication_acis(self):
        self._ldap_mod("replica-acis.ldif", self.sub_dict)

    def __setup_s4u2proxy(self):
        self._ldap_mod("replica-s4u2proxy.ldif", self.sub_dict)

    def __create_indices(self):
        self._ldap_mod("indices.ldif")

    def __certmap_conf(self):
        write_certmap_conf(self.realm, self.ca_subject)
        sysupgrade.set_upgrade_state(
            'certmap.conf',
            'subject_base',
            str(self.subject_base)
        )

    def __enable_ldapi(self):
        self._ldap_mod("ldapi.ldif", self.sub_dict,
                       ldap_uri="ldap://localhost",
                       dm_password=self.dm_password)

    def __enable_sasl_mapping_fallback(self):
        self._ldap_mod("sasl-mapping-fallback.ldif", self.sub_dict)

    def add_hbac(self):
        self._ldap_mod("default-hbac.ldif", self.sub_dict)

    def change_admin_password(self, password):
        root_logger.debug("Changing admin password")
        dmpwdfile = ""
        admpwdfile = ""

        try:
            (dmpwdfd, dmpwdfile) = tempfile.mkstemp(dir=paths.VAR_LIB_IPA)
            os.write(dmpwdfd, self.dm_password)
            os.close(dmpwdfd)

            (admpwdfd, admpwdfile) = tempfile.mkstemp(dir=paths.VAR_LIB_IPA)
            os.write(admpwdfd, password)
            os.close(admpwdfd)

            args = [paths.LDAPPASSWD, "-h", self.fqdn,
                    "-ZZ", "-x", "-D", str(DN(('cn', 'Directory Manager'))),
                    "-y", dmpwdfile, "-T", admpwdfile,
                    str(DN(('uid', 'admin'), ('cn', 'users'), ('cn', 'accounts'), self.suffix))]
            try:
                env = {'LDAPTLS_CACERTDIR': os.path.dirname(paths.IPA_CA_CRT),
                       'LDAPTLS_CACERT': paths.IPA_CA_CRT}
                ipautil.run(args, env=env)
                root_logger.debug("ldappasswd done")
            except ipautil.CalledProcessError as e:
                print("Unable to set admin password", e)
                root_logger.debug("Unable to set admin password %s" % e)

        finally:
            if os.path.isfile(dmpwdfile):
                os.remove(dmpwdfile)
            if os.path.isfile(admpwdfile):
                os.remove(admpwdfile)

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring directory server")

        enabled = self.restore_state("enabled")

        # Just eat this state if it exists
        self.restore_state("running")

        try:
            self.fstore.restore_file(paths.LIMITS_CONF)
            self.fstore.restore_file(paths.SYSCONFIG_DIRSRV)
        except ValueError as error:
            root_logger.debug(error)

        # disabled during IPA installation
        if enabled:
            self.enable()

        serverid = self.restore_state("serverid")
        if serverid is not None:
            self.stop_tracking_certificates(serverid)
            root_logger.debug("Removing DS instance %s" % serverid)
            try:
                remove_ds_instance(serverid)
                installutils.remove_keytab(paths.DS_KEYTAB)
                installutils.remove_ccache(run_as=DS_USER)
            except ipautil.CalledProcessError:
                root_logger.error("Failed to remove DS instance. You may "
                                  "need to remove instance data manually")

        # Just eat this state
        self.restore_state("user_exists")

        # Make sure some upgrade-related state is removed. This could cause
        # re-installation problems.
        self.restore_state('nsslapd-port')
        self.restore_state('nsslapd-security')
        self.restore_state('nsslapd-ldapiautobind')

        # If any dirsrv instances remain after we've removed ours then
        # (re)start them.
        for ds_instance in get_ds_instances():
            try:
                services.knownservices.dirsrv.restart(ds_instance, wait=False)
            except Exception as e:
                root_logger.error(
                    'Unable to restart DS instance %s: %s', ds_instance, e)

    def stop_tracking_certificates(self, serverid=None):
        if serverid is None:
            serverid = self.get_state("serverid")
        if not serverid is None:
            # drop the trailing / off the config_dirname so the directory
            # will match what is in certmonger
            dirname = config_dirname(serverid)[:-1]
            dsdb = certs.CertDB(self.realm, nssdir=dirname)
            dsdb.untrack_server_cert(self.nickname)

    def start_tracking_certificates(self, serverid):
        dirname = config_dirname(serverid)[:-1]
        dsdb = certs.CertDB(self.realm, nssdir=dirname)
        dsdb.track_server_cert(self.nickname, self.principal,
                               dsdb.passwd_fname,
                               'restart_dirsrv %s' % serverid)

    # we could probably move this function into the service.Service
    # class - it's very generic - all we need is a way to get an
    # instance of a particular Service
    def add_ca_cert(self, cacert_fname, cacert_name=''):
        """Add a CA certificate to the directory server cert db.  We
        first have to shut down the directory server in case it has
        opened the cert db read-only.  Then we use the CertDB class
        to add the CA cert.  We have to provide a nickname, and we
        do not use 'IPA CA' since that's the default, so
        we use 'Imported CA' if none specified.  Then we restart
        the server."""
        # first make sure we have a valid cacert_fname
        try:
            if not os.access(cacert_fname, os.R_OK):
                root_logger.critical("The given CA cert file named [%s] could not be read" %
                                             cacert_fname)
                return False
        except OSError as e:
            root_logger.critical("The given CA cert file named [%s] could not be read: %s" %
                                         (cacert_fname, str(e)))
            return False
        # ok - ca cert file can be read
        # shutdown the server
        self.stop()

        dirname = config_dirname(installutils.realm_to_serverid(self.realm))
        certdb = certs.CertDB(
            self.realm,
            nssdir=dirname,
            subject_base=self.subject_base,
            ca_subject=self.ca_subject,
        )
        if not cacert_name or len(cacert_name) == 0:
            cacert_name = "Imported CA"
        # we can't pass in the nickname, so we set the instance variable
        certdb.cacert_name = cacert_name
        status = True
        try:
            certdb.load_cacert(cacert_fname, 'C,,')
        except ipautil.CalledProcessError as e:
            root_logger.critical("Error importing CA cert file named [%s]: %s" %
                                         (cacert_fname, str(e)))
            status = False
        # restart the directory server
        self.start()

        return status

    def tune_nofile(self, num=8192):
        """
        Increase the number of files descriptors available to directory server
        from the default 1024 to 8192. This will allow to support a greater
        number of clients out of the box.
        """

        # Do the platform-specific changes
        proceed = services.knownservices.dirsrv.tune_nofile_platform(
                    num=num, fstore=self.fstore)

        if proceed:
            # finally change also DS configuration
            # NOTE: dirsrv will not allow you to set max file descriptors unless
            # the user limits allow it, so we have to restart dirsrv before
            # attempting to change them in cn=config
            self.__restart_instance()

            nf_sub_dict = dict(NOFILES=str(num))
            self._ldap_mod("ds-nfiles.ldif", nf_sub_dict)

    def __tuning(self):
        self.tune_nofile(8192)

    def __root_autobind(self):
        self._ldap_mod("root-autobind.ldif",
                       ldap_uri="ldap://localhost",
                       dm_password=self.dm_password)

    def __add_sudo_binduser(self):
        self._ldap_mod("sudobind.ldif", self.sub_dict)

    def __add_automember_config(self):
        self._ldap_mod("automember.ldif", self.sub_dict)

    def __add_replica_automember_config(self):
        self._ldap_mod("replica-automember.ldif", self.sub_dict)

    def __add_range_check_plugin(self):
        self._ldap_mod("range-check-conf.ldif", self.sub_dict)

    def _add_sidgen_plugin(self):
        """
        Add sidgen directory server plugin configuration if it does not already exist.
        """
        self.add_sidgen_plugin(self.sub_dict['SUFFIX'])

    def add_sidgen_plugin(self, suffix):
        """
        Add sidgen plugin configuration only if it does not already exist.
        """
        dn = DN('cn=IPA SIDGEN,cn=plugins,cn=config')
        try:
            api.Backend.ldap2.get_entry(dn)
        except errors.NotFound:
            self._ldap_mod('ipa-sidgen-conf.ldif', dict(SUFFIX=suffix))
        else:
            root_logger.debug("sidgen plugin is already configured")

    def _add_extdom_plugin(self):
        """
        Add directory server configuration for the extdom extended operation.
        """
        self.add_extdom_plugin(self.sub_dict['SUFFIX'])

    def add_extdom_plugin(self, suffix):
        """
        Add extdom configuration if it does not already exist.
        """
        dn = DN('cn=ipa_extdom_extop,cn=plugins,cn=config')
        try:
            api.Backend.ldap2.get_entry(dn)
        except errors.NotFound:
            self._ldap_mod('ipa-extdom-extop-conf.ldif', dict(SUFFIX=suffix))
        else:
            root_logger.debug("extdom plugin is already configured")

    def find_subject_base(self):
        """
        Try to find the current value of certificate subject base.
        1) Look in sysupgrade first
        2) If no value is found there, look in DS (start DS if necessary)
        3) If all fails, log loudly and return None

        Note that this method can only be executed AFTER the ipa server
        is configured, the api is initialized elsewhere and
        that a ticket already have been acquired.
        """
        root_logger.debug(
            'Trying to find certificate subject base in sysupgrade')
        subject_base = sysupgrade.get_upgrade_state(
            'certmap.conf', 'subject_base')

        if subject_base:
            root_logger.debug(
                'Found certificate subject base in sysupgrade: %s',
                subject_base)
            return subject_base

        root_logger.debug(
            'Unable to find certificate subject base in sysupgrade')
        root_logger.debug(
            'Trying to find certificate subject base in DS')

        ds_is_running = is_ds_running()
        if not ds_is_running:
            try:
                self.start()
                ds_is_running = True
            except ipautil.CalledProcessError as e:
                root_logger.error('Cannot start DS to find certificate '
                                  'subject base: %s', e)

        if ds_is_running:
            try:
                ret = api.Command['config_show']()
                subject_base = str(
                    ret['result']['ipacertificatesubjectbase'][0])
                root_logger.debug(
                    'Found certificate subject base in DS: %s', subject_base)
            except errors.PublicError as e:
                root_logger.error('Cannot connect to DS to find certificate '
                                  'subject base: %s', e)

        if subject_base:
            return subject_base

        root_logger.debug('Unable to find certificate subject base in '
                          'certmap.conf')
        return None

    def __set_domain_level(self):
        # Create global domain level entry and set the domain level
        if self.domainlevel is not None:
            self._ldap_mod("domainlevel.ldif", self.sub_dict)

    def _request_service_keytab(self):
        super(DsInstance, self)._request_service_keytab()

        # Configure DS to use the keytab
        vardict = {"KRB5_KTNAME": self.keytab}
        ipautil.config_replace_variables(paths.SYSCONFIG_DIRSRV,
                                         replacevars=vardict)

    def __get_ds_cert(self):
        nssdb_dir = config_dirname(self.serverid)
        db = certs.CertDB(
            self.realm,
            nssdir=nssdb_dir,
            subject_base=self.subject_base,
            ca_subject=self.ca_subject,
        )
        db.create_from_cacert(paths.IPA_CA_CRT)
        db.request_service_cert(self.nickname, self.principal, self.fqdn)
        db.create_pin_file()

        # Connect to self over ldapi as Directory Manager and configure SSL
        ldap_uri = ipaldap.get_ldap_uri(protocol='ldapi', realm=self.realm)
        conn = ipaldap.LDAPClient(ldap_uri)
        conn.external_bind()

        mod = [(ldap.MOD_REPLACE, "nsSSLClientAuth", "allowed"),
               (ldap.MOD_REPLACE, "nsSSL3Ciphers", "default"),
               (ldap.MOD_REPLACE, "allowWeakCipher", "off")]
        conn.modify_s(DN(('cn', 'encryption'), ('cn', 'config')), mod)

        mod = [(ldap.MOD_ADD, "nsslapd-security", "on")]
        conn.modify_s(DN(('cn', 'config')), mod)

        entry = conn.make_entry(
            DN(('cn', 'RSA'), ('cn', 'encryption'), ('cn', 'config')),
            objectclass=["top", "nsEncryptionModule"],
            cn=["RSA"],
            nsSSLPersonalitySSL=[self.nickname],
            nsSSLToken=["internal (software)"],
            nsSSLActivation=["on"],
        )
        conn.add_entry(entry)

        conn.unbind()

        # check for open secure port 636 from now on
        self.open_ports.append(636)


def write_certmap_conf(realm, ca_subject):
    """(Re)write certmap.conf with given CA subject DN."""
    serverid = installutils.realm_to_serverid(realm)
    ds_dirname = config_dirname(serverid)
    certmap_filename = os.path.join(ds_dirname, "certmap.conf")
    shutil.copyfile(
        os.path.join(paths.USR_SHARE_IPA_DIR, "certmap.conf.template"),
        certmap_filename)
    installutils.update_file(certmap_filename, '$ISSUER_DN', str(ca_subject))
