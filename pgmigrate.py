#!/usr/bin/env python
"""
PGmigrate - PostgreSQL migrations made easy
"""
# -*- coding: utf-8 -*-
#
#    Copyright (c) 2016-2020 Yandex LLC <https://github.com/yandex>
#    Copyright (c) 2016-2020 Other contributors as noted in the AUTHORS file.
#
#    Permission to use, copy, modify, and distribute this software and its
#    documentation for any purpose, without fee, and without a written
#    agreement is hereby granted, provided that the above copyright notice
#    and this paragraph and the following two paragraphs appear in all copies.
#
#    IN NO EVENT SHALL YANDEX LLC BE LIABLE TO ANY PARTY FOR DIRECT,
#    INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST
#    PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION,
#    EVEN IF YANDEX LLC HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
#    YANDEX SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING, BUT NOT
#    LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
#    PARTICULAR PURPOSE. THE SOFTWARE PROVIDED HEREUNDER IS ON AN "AS IS"
#    BASIS, AND YANDEX LLC HAS NO OBLIGATIONS TO PROVIDE MAINTENANCE,
#    SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.

from __future__ import absolute_import, print_function, unicode_literals

import argparse
import codecs
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from builtins import str as text
from collections import OrderedDict, namedtuple
from contextlib import closing

import psycopg2
import sqlparse
import yaml
from psycopg2.extensions import parse_dsn
from psycopg2.extras import LoggingConnection

LOG = logging.getLogger(__name__)


class MigrateError(RuntimeError):
    """
    Common migration error class
    """


class MalformedStatement(MigrateError):
    """
    Incorrect statement exception
    """


class MalformedMigration(MigrateError):
    """
    Incorrect migration exception
    """


class MalformedSchema(MigrateError):
    """
    Incorrect schema exception
    """


class ConfigurationError(MigrateError):
    """
    Incorrect config or cmd args exception
    """


class BaselineError(MigrateError):
    """
    Baseline error class
    """


def get_conn_id(conn):
    """
    Extract application_name from dsn
    """
    parsed = parse_dsn(conn.dsn)
    return parsed['application_name']


class ConflictTerminator(threading.Thread):
    """
    Kills conflicting pids (only on postgresql > 9.6)
    """
    def __init__(self, conn_str, interval):
        threading.Thread.__init__(self, name='terminator')
        self.daemon = True
        self.log = logging.getLogger('terminator')
        self.conn_str = conn_str
        self.conns = set()
        self.interval = interval
        self.should_run = True
        self.conn = None

    def stop(self):
        """
        Stop iterations and close connection
        """
        self.should_run = False

    def add_conn(self, conn):
        """
        Add conn pid to pgmirate pids list
        """
        self.conns.add(get_conn_id(conn))

    def remove_conn(self, conn):
        """
        Remove conn from pgmigrate pids list
        """
        self.conns.remove(get_conn_id(conn))

    def run(self):
        """
        Periodically terminate all backends blocking pgmigrate pids
        """
        self.conn = _create_raw_connection(self.conn_str, self.log)
        self.conn.autocommit = True
        while self.should_run:
            with self.conn.cursor() as cursor:
                for conn_id in self.conns:
                    cursor.execute(
                        """
                        SELECT b.blocking_pid,
                               pg_terminate_backend(b.blocking_pid)
                        FROM (SELECT unnest(pg_blocking_pids(pid))
                              AS blocking_pid
                              FROM pg_stat_activity
                              WHERE application_name
                                  LIKE '%%' || %s || '%%') as b
                        """, (conn_id, ))
                    terminated = [x[0] for x in cursor.fetchall()]
                    for i in terminated:
                        self.log.info('Terminated conflicting pid: %s', i)
            time.sleep(self.interval)


REF_COLUMNS = [
    'version',
    'description',
    'type',
    'installed_by',
    'installed_on',
]


def _create_raw_connection(conn_string, logger=LOG):
    conn = psycopg2.connect(conn_string, connection_factory=LoggingConnection)
    conn.initialize(logger)

    return conn


def _create_connection(config):
    conn_id = 'pgmigrate-{id}'.format(id=str(uuid.uuid4()))
    conn = _create_raw_connection('{dsn} application_name={conn_id}'.format(
        dsn=config.conn, conn_id=conn_id))
    if config.terminator_instance:
        config.terminator_instance.add_conn(conn)

    return conn


def _init_cursor(conn, session):
    """
    Get cursor initialized with session commands
    """
    cursor = conn.cursor()
    for query in session:
        cursor.execute(query)
        LOG.info(cursor.statusmessage)

    return cursor


def _is_initialized(cursor):
    """
    Check that database is initialized
    """
    cursor.execute(
        'SELECT EXISTS(SELECT 1 FROM '
        'information_schema.tables '
        'WHERE table_schema = %s '
        'AND table_name = %s)', ('public', 'schema_version'))
    table_exists = cursor.fetchone()[0]

    if not table_exists:
        return False

    cursor.execute('SELECT * from public.schema_version limit 1')

    colnames = [desc[0] for desc in cursor.description]

    if colnames != REF_COLUMNS:
        raise MalformedSchema(
            'Table schema_version has unexpected '
            'structure: {struct}'.format(struct='|'.join(colnames)))

    return True


MIGRATION_FILE_RE = re.compile(r'V(?P<version>\d+)__(?P<description>.+)\.sql$')

MigrationInfo = namedtuple('MigrationInfo', ('meta', 'file_path'))

Callbacks = namedtuple('Callbacks',
                       ('beforeAll', 'beforeEach', 'afterEach', 'afterAll'))

Config = namedtuple('Config',
                    ('target', 'baseline', 'cursor', 'dryrun', 'callbacks',
                     'user', 'base_dir', 'conn', 'session', 'conn_instance',
                     'terminator_instance', 'termination_interval'))

CONFIG_IGNORE = ['cursor', 'conn_instance', 'terminator_instance']


def _get_files_from_dir(path):
    """
    Get all files in all subdirs in path
    """
    for root, _, files in os.walk(path):
        for fname in files:
            yield os.path.basename(fname), os.path.join(root, fname)


def _get_migrations_info_from_dir(base_dir):
    """
    Get all migrations from base dir
    """
    path = os.path.join(base_dir, 'migrations')
    migrations = {}
    if not (os.path.exists(path) and os.path.isdir(path)):
        raise ConfigurationError(
            'Migrations dir not found (expected to be {path})'.format(
                path=path))
    for fname, file_path in _get_files_from_dir(path):
        match = MIGRATION_FILE_RE.match(fname)
        if match is None:
            LOG.warning('File %s does not match by pattern %s. Skipping it.',
                        file_path, MIGRATION_FILE_RE.pattern)
            continue
        version = int(match.group('version'))
        ret = dict(
            version=version,
            type='auto',
            installed_by=None,
            installed_on=None,
            description=match.group('description').replace('_', ' '),
        )
        ret['transactional'] = 'NONTRANSACTIONAL' not in ret['description']
        migration = MigrationInfo(
            ret,
            file_path,
        )
        if version in migrations:
            raise MalformedMigration(
                ('Found migrations with same version: {version} '
                 '\nfirst : {first_path}'
                 '\nsecond: {second_path}').format(
                     version=version,
                     first_path=migration.file_path,
                     second_path=migrations[version].file_path))
        migrations[version] = migration

    return migrations


def _get_migrations_info(base_dir, baseline_v, target_v):
    """
    Get migrations from baseline to target from base dir
    """
    migrations = {}
    target = target_v if target_v is not None else float('inf')

    for version, ret in _get_migrations_info_from_dir(base_dir).items():
        if baseline_v < version <= target:
            migrations[version] = ret.meta
        else:
            LOG.info(
                'Ignore migration %r cause baseline: %r or target: %r',
                ret,
                baseline_v,
                target,
            )
    return migrations


def _get_info(base_dir, baseline_v, target_v, cursor):
    """
    Get migrations info from database and base dir
    """
    ret = {}
    cursor.execute('SELECT {columns} FROM public.schema_version'.format(
        columns=', '.join(REF_COLUMNS)))
    for i in cursor.fetchall():
        version = {}
        for j in enumerate(REF_COLUMNS):
            if j[1] == 'installed_on':
                version[j[1]] = i[j[0]].strftime('%F %H:%M:%S')
            else:
                version[j[1]] = i[j[0]]
        version['version'] = int(version['version'])
        transactional = 'NONTRANSACTIONAL' not in version['description']
        version['transactional'] = transactional
        ret[version['version']] = version

        baseline_v = max(baseline_v, sorted(ret.keys())[-1])

    migrations_info = _get_migrations_info(base_dir, baseline_v, target_v)
    for version in migrations_info:
        num = migrations_info[version]['version']
        if num not in ret:
            ret[num] = migrations_info[version]

    return ret


def _get_database_user(cursor):
    cursor.execute('SELECT CURRENT_USER')
    return cursor.fetchone()[0]


def _get_state(base_dir, baseline_v, target, cursor):
    """
    Get info wrapper (able to handle noninitialized database)
    """
    if _is_initialized(cursor):
        return _get_info(base_dir, baseline_v, target, cursor)
    return _get_migrations_info(base_dir, baseline_v, target)


def _set_baseline(baseline_v, user, cursor):
    """
    Cleanup schema_version and set baseline
    """
    cursor.execute(
        'SELECT EXISTS(SELECT 1 FROM public'
        '.schema_version WHERE version >= %s::bigint)', (baseline_v, ))
    check_failed = cursor.fetchone()[0]

    if check_failed:
        raise BaselineError(
            'Unable to baseline, version '
            '{version} already applied'.format(version=text(baseline_v)))

    LOG.info('cleaning up table schema_version')
    cursor.execute('DELETE FROM public.schema_version')
    LOG.info(cursor.statusmessage)

    LOG.info('setting baseline')
    cursor.execute(
        'INSERT INTO public.schema_version '
        '(version, type, description, installed_by) '
        'VALUES (%s::bigint, %s, %s, %s)',
        (text(baseline_v), 'manual', 'Forced baseline', user))
    LOG.info(cursor.statusmessage)


def _init_schema(cursor):
    """
    Create schema_version table
    """
    LOG.info('creating type schema_version_type')
    cursor.execute(
        'CREATE TYPE public.schema_version_type '
        'AS ENUM (%s, %s)', ('auto', 'manual'))
    LOG.info(cursor.statusmessage)
    LOG.info('creating table schema_version')
    cursor.execute(
        'CREATE TABLE public.schema_version ('
        'version BIGINT NOT NULL PRIMARY KEY, '
        'description TEXT NOT NULL, '
        'type public.schema_version_type NOT NULL '
        'DEFAULT %s, '
        'installed_by TEXT NOT NULL, '
        'installed_on TIMESTAMP WITHOUT time ZONE '
        'DEFAULT now() NOT NULL)', ('auto', ))
    LOG.info(cursor.statusmessage)


def _get_statements(path):
    """
    Get statements from file
    """
    with codecs.open(path, encoding='utf-8') as i:
        data = i.read()
    if u'/* pgmigrate-encoding: utf-8 */' not in data:
        try:
            data.encode('ascii')
        except UnicodeError as exc:
            raise MalformedStatement(
                'Non ascii symbols in file: {0}, {1}'.format(path, text(exc)))
    data = sqlparse.format(data, strip_comments=True)
    for statement in sqlparse.parsestream(data, encoding='utf-8'):
        st_str = text(statement).strip().encode('utf-8')
        if st_str:
            yield st_str


def _apply_statement(statement, cursor):
    """
    Execute statement using cursor
    """
    try:
        cursor.execute(statement)
    except psycopg2.Error as exc:
        LOG.error('Error executing statement:')
        for line in statement.splitlines():
            LOG.error(line)
        LOG.error(exc)
        raise MigrateError('Unable to apply statement')


def _apply_file(file_path, cursor):
    """
    Execute all statements in file
    """
    try:
        for statement in _get_statements(file_path):
            _apply_statement(statement, cursor)
    except MalformedStatement as exc:
        LOG.error(exc)
        raise exc


def _apply_version(version, base_dir, user, cursor):
    """
    Execute all statements in migration version
    """
    all_versions = _get_migrations_info_from_dir(base_dir)
    version_info = all_versions[version]
    LOG.info('Try apply version %r', version_info)

    _apply_file(version_info.file_path, cursor)
    cursor.execute(
        'INSERT INTO public.schema_version '
        '(version, description, installed_by) '
        'VALUES (%s::bigint, %s, %s)',
        (text(version), version_info.meta['description'], user))


def _parse_str_callbacks(callbacks, ret, base_dir):
    callbacks = callbacks.split(',')
    for callback in callbacks:
        if not callback:
            continue
        tokens = callback.split(':')
        if tokens[0] not in ret._fields:
            raise ConfigurationError(
                'Unexpected callback '
                'type: {type}'.format(type=text(tokens[0])))
        path = os.path.join(base_dir, tokens[1])
        if not os.path.exists(path):
            raise ConfigurationError(
                'Path unavailable: {path}'.format(path=text(path)))
        if os.path.isdir(path):
            for fname in sorted(os.listdir(path)):
                getattr(ret, tokens[0]).append(os.path.join(path, fname))
        else:
            getattr(ret, tokens[0]).append(path)

    return ret


def _parse_dict_callbacks(callbacks, ret, base_dir):
    for i in callbacks:
        if i in ret._fields:
            for j in callbacks[i]:
                path = os.path.join(base_dir, j)
                if not os.path.exists(path):
                    raise ConfigurationError(
                        'Path unavailable: {path}'.format(path=text(path)))
                if os.path.isdir(path):
                    for fname in sorted(os.listdir(path)):
                        getattr(ret, i).append(os.path.join(path, fname))
                else:
                    getattr(ret, i).append(path)
        else:
            raise ConfigurationError(
                'Unexpected callback type: {type}'.format(type=text(i)))

    return ret


def _get_callbacks(callbacks, base_dir=''):
    """
    Parse cmdline/config callbacks
    """
    ret = Callbacks(beforeAll=[], beforeEach=[], afterEach=[], afterAll=[])
    if isinstance(callbacks, dict):
        return _parse_dict_callbacks(callbacks, ret, base_dir)
    return _parse_str_callbacks(callbacks, ret, base_dir)


def _migrate_step(state, callbacks, base_dir, user, cursor):
    """
    Apply one version with callbacks
    """
    before_all_executed = False
    should_migrate = False
    if not _is_initialized(cursor):
        LOG.info('schema not initialized')
        _init_schema(cursor)
    for version in sorted(state.keys()):
        LOG.debug('has version %r', version)
        if state[version]['installed_on'] is None:
            should_migrate = True
            if not before_all_executed and callbacks.beforeAll:
                LOG.info('Executing beforeAll callbacks:')
                for callback in callbacks.beforeAll:
                    _apply_file(callback, cursor)
                    LOG.info(callback)
                before_all_executed = True

            LOG.info('Migrating to version %d', version)
            if callbacks.beforeEach:
                LOG.info('Executing beforeEach callbacks:')
                for callback in callbacks.beforeEach:
                    LOG.info(callback)
                    _apply_file(callback, cursor)

            _apply_version(version, base_dir, user, cursor)

            if callbacks.afterEach:
                LOG.info('Executing afterEach callbacks:')
                for callback in callbacks.afterEach:
                    LOG.info(callback)
                    _apply_file(callback, cursor)

    if should_migrate and callbacks.afterAll:
        LOG.info('Executing afterAll callbacks:')
        for callback in callbacks.afterAll:
            LOG.info(callback)
            _apply_file(callback, cursor)


def _finish(config):
    if config.dryrun:
        config.cursor.execute('rollback')
    else:
        config.cursor.execute('commit')
    if config.terminator_instance:
        config.terminator_instance.stop()
    config.conn_instance.close()


def info(config, stdout=True):
    """
    Info cmdline wrapper
    """
    state = _get_state(config.base_dir, config.baseline, config.target,
                       config.cursor)
    if stdout:
        out_state = OrderedDict()
        for version in sorted(state, key=int):
            out_state[version] = state[version]
        sys.stdout.write(
            json.dumps(out_state, indent=4, separators=(',', ': ')) + '\n')

    _finish(config)

    return state


def clean(config):
    """
    Drop schema_version table
    """
    if _is_initialized(config.cursor):
        LOG.info('dropping schema_version')
        config.cursor.execute('DROP TABLE public.schema_version')
        LOG.info(config.cursor.statusmessage)
        LOG.info('dropping schema_version_type')
        config.cursor.execute('DROP TYPE public.schema_version_type')
        LOG.info(config.cursor.statusmessage)
        _finish(config)


def baseline(config):
    """
    Set baseline cmdline wrapper
    """
    if not _is_initialized(config.cursor):
        _init_schema(config.cursor)
    _set_baseline(config.baseline, config.user, config.cursor)

    _finish(config)


def _prepare_nontransactional_steps(state, callbacks):
    steps = []
    i = {'state': {}, 'cbs': _get_callbacks('')}
    for version in sorted(state):
        if not state[version]['transactional']:
            if i['state']:
                steps.append(i)
                i = {'state': {}, 'cbs': _get_callbacks('')}
            elif not steps:
                LOG.error('First migration MUST be transactional')
                raise MalformedMigration('First migration MUST '
                                         'be transactional')
            steps.append({
                'state': {
                    version: state[version],
                },
                'cbs': _get_callbacks(''),
            })
        else:
            i['state'][version] = state[version]
            i['cbs'] = callbacks

    if i['state']:
        steps.append(i)

    transactional = []
    for (num, step) in enumerate(steps):
        if list(step['state'].values())[0]['transactional']:
            transactional.append(num)

    if len(transactional) > 1:
        for num in transactional[1:]:
            steps[num]['cbs'] = steps[num]['cbs']._replace(beforeAll=[])
        for num in transactional[:-1]:
            steps[num]['cbs'] = steps[num]['cbs']._replace(afterAll=[])

    LOG.info('Initialization plan result:\n %s',
             json.dumps(steps, indent=4, separators=(',', ': ')))

    return steps


def _execute_mixed_steps(config, steps, nt_conn):
    commit_req = False
    for step in steps:
        if commit_req:
            config.cursor.execute('commit')
            commit_req = False
        if not list(step['state'].values())[0]['transactional']:
            cur = _init_cursor(nt_conn, config.session)
        else:
            cur = config.cursor
            commit_req = True
        _migrate_step(step['state'], step['cbs'], config.base_dir, config.user,
                      cur)


def migrate(config):
    """
    Migrate cmdline wrapper
    """
    if config.target is None:
        LOG.error('Unknown target (you could use "latest" to '
                  'use latest available version)')
        raise MigrateError('Unknown target')

    state = _get_state(config.base_dir, config.baseline, config.target,
                       config.cursor)
    not_applied = [x for x in state if state[x]['installed_on'] is None]
    non_trans = [x for x in not_applied if not state[x]['transactional']]

    if non_trans:
        if config.dryrun:
            LOG.error('Dry run for nontransactional migrations ' 'is nonsence')
            raise MigrateError('Dry run for nontransactional migrations '
                               'is nonsence')
        if len(state) != len(not_applied):
            if len(not_applied) != len(non_trans):
                LOG.error('Unable to mix transactional and '
                          'nontransactional migrations')
                raise MigrateError('Unable to mix transactional and '
                                   'nontransactional migrations')
            config.cursor.execute('rollback')
            with closing(_create_connection(config)) as nt_conn:
                nt_conn.autocommit = True
                cursor = _init_cursor(nt_conn, config.session)
                _migrate_step(state, _get_callbacks(''), config.base_dir,
                              config.user, cursor)
                if config.terminator_instance:
                    config.terminator_instance.remove_conn(nt_conn)
        else:
            steps = _prepare_nontransactional_steps(state, config.callbacks)

            with closing(_create_connection(config)) as nt_conn:
                nt_conn.autocommit = True

                _execute_mixed_steps(config, steps, nt_conn)

                if config.terminator_instance:
                    config.terminator_instance.remove_conn(nt_conn)
    else:
        _migrate_step(state, config.callbacks, config.base_dir, config.user,
                      config.cursor)

    _finish(config)


COMMANDS = {
    'info': info,
    'clean': clean,
    'baseline': baseline,
    'migrate': migrate,
}

CONFIG_DEFAULTS = Config(target=None,
                         baseline=0,
                         cursor=None,
                         dryrun=False,
                         callbacks='',
                         base_dir='',
                         user=None,
                         session=['SET lock_timeout = 0'],
                         conn='dbname=postgres user=postgres '
                         'connect_timeout=1',
                         conn_instance=None,
                         terminator_instance=None,
                         termination_interval=None)


def get_config(base_dir, args=None):
    """
    Load configuration from yml in base dir with respect of args
    """
    path = os.path.join(base_dir, 'migrations.yml')
    try:
        with codecs.open(path, encoding='utf-8') as i:
            base = yaml.safe_load(i)
    except IOError:
        LOG.info('Unable to load %s. Using defaults', path)
        base = {}

    conf = CONFIG_DEFAULTS
    for i in [j for j in CONFIG_DEFAULTS._fields if j not in CONFIG_IGNORE]:
        if i in base:
            conf = conf._replace(**{i: base[i]})
        if args is not None:
            if i in args.__dict__ and args.__dict__[i] is not None:
                conf = conf._replace(**{i: args.__dict__[i]})

    if conf.target is not None:
        if conf.target == 'latest':
            conf = conf._replace(target=float('inf'))
        else:
            conf = conf._replace(target=int(conf.target))

    if conf.termination_interval and not conf.dryrun:
        conf = conf._replace(terminator_instance=ConflictTerminator(
            conf.conn, conf.termination_interval))
        conf.terminator_instance.start()

    conf = conf._replace(conn_instance=_create_connection(conf))
    conf = conf._replace(cursor=_init_cursor(conf.conn_instance, conf.session))
    conf = conf._replace(
        callbacks=_get_callbacks(conf.callbacks, conf.base_dir))

    if conf.user is None:
        conf = conf._replace(user=_get_database_user(conf.cursor))
    elif not conf.user:
        raise ConfigurationError('Empty user name')

    return conf


def _main():
    """
    Main function
    """
    parser = argparse.ArgumentParser()

    parser.add_argument('cmd',
                        choices=COMMANDS.keys(),
                        type=str,
                        help='Operation')
    parser.add_argument('-t', '--target', type=str, help='Target version')
    parser.add_argument('-c',
                        '--conn',
                        type=str,
                        help='Postgresql connection string')
    parser.add_argument('-d',
                        '--base_dir',
                        type=str,
                        default='',
                        help='Migrations base dir')
    parser.add_argument('-u',
                        '--user',
                        type=str,
                        help='Override database user in migration info')
    parser.add_argument('-b', '--baseline', type=int, help='Baseline version')
    parser.add_argument('-a',
                        '--callbacks',
                        type=str,
                        help='Comma-separated list of callbacks '
                        '(type:dir/file)')
    parser.add_argument('-s',
                        '--session',
                        action='append',
                        help='Session setup (e.g. isolation level)')
    parser.add_argument('-n',
                        '--dryrun',
                        action='store_true',
                        help='Say "rollback" in the end instead of "commit"')
    parser.add_argument('-l',
                        '--termination_interval',
                        type=float,
                        help='Inverval for terminating blocking pids')
    parser.add_argument('-v',
                        '--verbose',
                        default=0,
                        action='count',
                        help='Be verbose')

    args = parser.parse_args()
    logging.basicConfig(level=(logging.ERROR - 10 * (min(3, args.verbose))),
                        format='%(asctime)s %(levelname)-8s: %(message)s')

    config = get_config(args.base_dir, args)

    COMMANDS[args.cmd](config)


if __name__ == '__main__':
    _main()
