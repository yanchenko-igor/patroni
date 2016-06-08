import json
import logging
import os
import tempfile
import yaml

from collections import defaultdict
from copy import deepcopy
from patroni.dcs import ClusterConfig
from patroni.postgresql import Postgresql
from patroni.utils import deep_compare, patch_config

logger = logging.getLogger(__name__)


class Config(object):
    """
    This class is responsible for:

      1) Building and giving access to `effective_configuration` from:
         * `Config.__DEFAULT_CONFIG` -- some sane default values
         * `dynamic_configuration` -- configuration stored in DCS
         * `local_configuration` -- configuration from `config.yml` or environment

      2) Saving and loading `dynamic_configuration` into 'patroni.dynamic.json' file
         located in local_configuration['postgresql']['data_dir'] directory.
         This is necessary to be able to restore `dynamic_configuration`
         if DCS was accidentally wiped

      3) Loading of configuration file in the old format and converting it into new format

      4) Mimicking some of the `dict` interfaces to make it possible
         to work with it as with the old `config` object.
    """

    __CACHE_FILENAME = 'patroni.dynamic.json'
    __DEFAULT_CONFIG = {
        'ttl': 30, 'loop_wait': 10, 'retry_timeout': 10,
        'maximum_lag_on_failover': 1048576,
        'postgresql': {
            'parameters': Postgresql.CMDLINE_OPTIONS
        }
    }

    def __init__(self, config_file=None, config_env=None):
        self._config_file = None if config_env else config_file
        self._modify_index = -1
        self._dynamic_configuration = {}
        if config_env:
            self._local_configuration = yaml.safe_load(config_env)
        else:
            self.__environment_configuration = self._build_environment_configuration()
            self._local_configuration = self._load_config_file()
        self.__effective_configuration = self._build_effective_configuration(self._dynamic_configuration,
                                                                             self._local_configuration)
        self._data_dir = self.__effective_configuration['postgresql']['data_dir']
        self._cache_file = os.path.join(self._data_dir, self.__CACHE_FILENAME)
        self._load_cache()
        self._cache_needs_saving = False

    @property
    def config_file(self):
        return self._config_file

    @property
    def dynamic_configuration(self):
        return deepcopy(self._dynamic_configuration)

    def _load_config_file(self):
        """Loads config.yaml from filesystem and applies some values which were set via ENV"""
        with open(self._config_file) as f:
            config = yaml.safe_load(f)
            patch_config(config, self.__environment_configuration)
            return config

    def _load_cache(self):
        if os.path.isfile(self._cache_file):
            try:
                with open(self._cache_file) as f:
                    self.set_dynamic_configuration(json.load(f))
            except Exception:
                logger.exception('Exception when loading file: %s', self._cache_file)

    def save_cache(self):
        if self._cache_needs_saving:
            tmpfile = fd = None
            try:
                (fd, tmpfile) = tempfile.mkstemp(prefix=self.__CACHE_FILENAME, dir=self._data_dir)
                with os.fdopen(fd, 'w') as f:
                    fd = None
                    json.dump(self.dynamic_configuration, f)
                tmpfile = os.rename(tmpfile, self._cache_file)
                self._cache_needs_saving = False
            except Exception:
                logger.exception('Exception when saving file: %s', self._cache_file)
                if fd:
                    try:
                        os.close(fd)
                    except Exception:
                        logger.error('Can not close temporary file %s', tmpfile)
                if tmpfile and os.path.exists(tmpfile):
                    try:
                        os.remove(tmpfile)
                    except Exception:
                        logger.error('Can not remove temporary file %s', tmpfile)

    # configuration could be either ClusterConfig or dict
    def set_dynamic_configuration(self, configuration):
        if isinstance(configuration, ClusterConfig):
            if self._modify_index == configuration.modify_index:
                return False  # If the index didn't changed there is nothing to do
            self._modify_index = configuration.modify_index
            configuration = configuration.data

        if not deep_compare(self._dynamic_configuration, configuration):
            try:
                self.__effective_configuration = self._build_effective_configuration(configuration,
                                                                                     self._local_configuration)
                self._dynamic_configuration = configuration
                self._cache_needs_saving = True
                return True
            except Exception:
                logger.exception('Exception when setting dynamic_configuration')

    def reload_local_configuration(self, dry_run=False):
        if self.config_file:
            try:
                configuration = self._load_config_file()
                if not deep_compare(self._local_configuration, configuration):
                    new_configuration = self._build_effective_configuration(self._dynamic_configuration, configuration)
                    if dry_run:
                        return not deep_compare(new_configuration, self.__effective_configuration)
                    self._local_configuration = configuration
                    self.__effective_configuration = new_configuration
                    return True
            except Exception:
                logger.exception('Exception when reloading local configuration from %s', self.config_file)
                if dry_run:
                    raise

    def _process_postgresql_parameters(self, parameters, is_local=False):
        ret = {}
        for name, value in (parameters or {}).items():
            if (is_local and name not in self.__DEFAULT_CONFIG['postgresql']['parameters']) \
                or not ((name == 'wal_level' and value not in ('hot_standby', 'logical')) or
                        (name in ('max_replication_slots', 'max_wal_senders', 'wal_keep_segments') and
                         int(value) < self.__DEFAULT_CONFIG['postgresql']['parameters'][name]) or
                        name in ('hot_standby', 'wal_log_hints')):
                ret[name] = value
        return ret

    def _safe_copy_dynamic_configuration(self, dynamic_configuration):
        config = deepcopy(self.__DEFAULT_CONFIG)

        for name, value in dynamic_configuration.items():
            if name == 'postgresql':
                for name, value in (value or {}).items():
                    if name == 'parameters':
                        config['postgresql'][name].update(self._process_postgresql_parameters(value))
                    elif name not in ('connect_address', 'listen', 'data_dir', 'pgpass', 'authentication'):
                        config['postgresql'][name] = deepcopy(value)
            elif name in config:  # only variables present in __DEFAULT_CONFIG allowed to be overriden from DCS
                config[name] = int(value)
        return config

    @staticmethod
    def _build_environment_configuration():
        ret = defaultdict(dict)

        def _popenv(name):
            return os.environ.pop('PATRONI_' + name.upper(), None)

        for param in ('name', 'namespace', 'scope'):
            value = _popenv(param)
            if value:
                ret[param] = value

        def _set_section_values(section, params):
            for param in params:
                value = _popenv(section + '_' + param)
                if value:
                    ret[section][param] = value

        _set_section_values('restapi', ['listen', 'connect_address', 'certfile', 'keyfile'])
        _set_section_values('postgresql', ['listen', 'connect_address', 'data_dir', 'pgpass'])

        def _get_auth(name):
            ret = {}
            for param in ('username', 'password'):
                value = _popenv(name + '_' + param)
                if value:
                    ret[param] = value
            return len(ret) == 2 and ret or None

        restapi_auth = _get_auth('restapi')
        if restapi_auth:
            ret['restapi']['authentication'] = restapi_auth

        authentication = {}
        for user_type in ('replication', 'superuser'):
            entry = _get_auth(user_type)
            if entry:
                authentication[user_type] = entry

        if authentication:
            ret['postgresql']['authentication'] = authentication

        users = {}

        def _parse_list(value):
            if not (value.strip().startswith('-') or '[' in value):
                value = '[{0}]'.format(value)
            try:
                return yaml.safe_load(value)
            except Exception:
                return None

        for param in list(os.environ.keys()):
            if param.startswith('PATRONI_'):
                name, suffix = (param[8:].rsplit('_', 1) + [''])[:2]
                if name and suffix:
                    # PATRONI_(ETCD|CONSUL|ZOOKEEPER|...)_HOSTS?
                    if suffix in ('HOST', 'HOSTS') and '_' not in name:
                        value = os.environ.pop(param)
                        value = value if suffix == 'HOST' else value and _parse_list(value)
                        if value:
                            ret[name.lower()][suffix.lower()] = value
                    # PATRONI_<username>_PASSWORD=<password>, PATRONI_<username>_OPTIONS=<option1,option2,...>
                    # CREATE USER "<username>" WITH <OPTIONS> PASSWORD '<password>'
                    elif suffix == 'PASSWORD':
                        password = os.environ.pop(param)
                        if password:
                            users[name] = {'password': password}
                            options = os.environ.pop(param[:-9] + '_OPTIONS', None)
                            options = options and _parse_list(options)
                            if options:
                                users[name]['options'] = options
        if users:
            ret['bootstrap']['users'] = users

        return ret

    def _build_effective_configuration(self, dynamic_configuration, local_configuration):
        config = self._safe_copy_dynamic_configuration(dynamic_configuration)
        for name, value in local_configuration.items():
            if name == 'postgresql':
                for name, value in (value or {}).items():
                    if name == 'parameters':
                        config['postgresql'][name].update(self._process_postgresql_parameters(value, True))
                    else:
                        config['postgresql'][name] = deepcopy(value)
            elif name not in config:
                config[name] = deepcopy(value) if value else {}

        if 'authentication' in config['restapi']:
            restapi = config['restapi']
            auth = restapi['authentication']
            restapi['auth'] = '{0}:{1}'.format(auth['username'], auth['password'])

        pg_config = config['postgresql']

        # special treatment for old config
        if 'authentication' not in pg_config:
            pg_config['use_pg_rewind'] = 'pg_rewind' in pg_config
            pg_config['authentication'] = {u: pg_config[u] for u in ('replication', 'superuser') if u in pg_config}

        if 'superuser' not in pg_config['authentication'] and 'pg_rewind' in pg_config:
            pg_config['authentication']['superuser'] = pg_config['pg_rewind']

        if 'name' not in config and 'name' in pg_config:
            config['name'] = pg_config['name']

        pg_config.update({p: config[p] for p in ('name', 'scope', 'retry_timeout',
                          'maximum_lag_on_failover') if p in config})

        return config

    def get(self, key, default=None):
        return self.__effective_configuration.get(key, default)

    def __contains__(self, key):
        return key in self.__effective_configuration

    def __getitem__(self, key):
        return self.__effective_configuration[key]
