import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import requests

CLIENT_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'Client')
sys.path.insert(0, os.path.abspath(CLIENT_DIR))

import utils
import worker

import client

CLIENT_ARGS = argparse.Namespace(username='lab-worker', password='secret', server='https://ob.invalid')


def parse(*argv):
    with mock.patch.object(sys, 'argv', ['client.py', '-T', '4', '-N', '1', *argv]):
        return worker.parse_arguments(CLIENT_ARGS)


def config_with(workload):
    return argparse.Namespace(
        workload=workload, single=True, blacklist=[], machine_id=1, secret_token='t', server='https://ob.invalid'
    )


WORKLOAD = {'test': {'id': 7}}


class ArgumentTests(unittest.TestCase):
    def test_defaults(self):
        args = parse()
        self.assertFalse(args.single_workload)
        self.assertIsNone(args.blacklist)

    def test_single_workload_and_blacklist(self):
        args = parse('--single-workload', '--blacklist', '12,34')
        self.assertTrue(args.single_workload)
        self.assertEqual(args.blacklist, [12, 34])

    def test_malformed_blacklist_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parse('--blacklist', '12,abc')
        self.assertEqual(raised.exception.code, 2)

    def test_blacklist_reaches_the_workload_request(self):
        config = config_with(None)
        config.blacklist = parse('--blacklist', '12,34').blacklist
        response = mock.Mock(**{'json.return_value': {}})
        with (
            mock.patch.object(worker.requests, 'post', return_value=response) as post,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            worker.server_request_workload(config)
        self.assertEqual(post.call_args.kwargs['data']['blacklist'], [12, 34])


class SingleWorkloadTests(unittest.TestCase):
    def run_single(self, workload, outcome=None):
        stdout = io.StringIO()
        with (
            mock.patch.object(worker, 'complete_workload', side_effect=outcome),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            worker.complete_single_workload(config_with(workload))
        return raised.exception.code, stdout.getvalue()

    def test_no_workload_exits_3(self):
        self.assertEqual(self.run_single(None)[0], 3)

    def test_completed_workload_exits_0(self):
        code, stdout = self.run_single(WORKLOAD)
        self.assertEqual(code, 0)
        self.assertNotIn('FAILED_TEST', stdout)

    def test_build_failure_exits_4_naming_the_test(self):
        code, stdout = self.run_single(WORKLOAD, utils.OpenBenchBuildFailedException('build', 'logs'))
        self.assertEqual(code, 4)
        self.assertIn('FAILED_TEST 7\n', stdout)

    def test_bad_bench_exits_4(self):
        self.assertEqual(self.run_single(WORKLOAD, utils.OpenBenchBadBenchException('bench'))[0], 4)

    def test_unreachable_server_exits_5_without_blaming_the_test(self):
        for error in [
            requests.exceptions.ConnectionError(),
            utils.OpenBenchBadServerResponseException(),
            utils.OpenBenchFatalWorkerException('x'),
        ]:
            with self.subTest(error=type(error).__name__):
                code, stdout = self.run_single(WORKLOAD, error)
                self.assertEqual(code, 5)
                self.assertNotIn('FAILED_TEST', stdout)

    def test_interrupt_exits_130(self):
        self.assertEqual(self.run_single(WORKLOAD, KeyboardInterrupt())[0], 130)

    def test_version_change_propagates_to_the_client(self):
        with (
            mock.patch.object(worker, 'complete_workload', side_effect=client.BadVersionException()),
            self.assertRaises(client.BadVersionException),
        ):
            worker.complete_single_workload(config_with(WORKLOAD))


class WorkerLoopTests(unittest.TestCase):
    def test_loop_exits_after_the_first_request(self):
        config = config_with(None)

        def request(config):
            config.workload = None

        with (
            mock.patch.object(worker, 'parse_arguments', return_value=argparse.Namespace(print_system_info=False)),
            mock.patch.object(worker, 'Configuration', return_value=config),
            mock.patch.object(worker, 'reload_local_imports'),
            mock.patch.object(worker, 'server_configure_fastchess'),
            mock.patch.object(worker, 'server_configure_worker'),
            mock.patch.object(worker, 'set_runner_permissions'),
            mock.patch.object(worker, 'cleanup_client'),
            mock.patch.object(worker, 'server_request_workload', side_effect=request) as requested,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            worker.run_openbench_worker(CLIENT_ARGS)

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(requested.call_count, 1)


class SetupExitTests(unittest.TestCase):
    def test_interrupt_before_the_workload_is_not_success(self):
        config = config_with(None)
        with (
            mock.patch.object(worker, 'parse_arguments', return_value=argparse.Namespace(print_system_info=False)),
            mock.patch.object(worker, 'Configuration', return_value=config),
            mock.patch.object(worker, 'reload_local_imports'),
            mock.patch.object(worker, 'server_configure_fastchess', side_effect=KeyboardInterrupt),
            mock.patch.object(worker.signal, 'signal'),
            self.assertRaises(KeyboardInterrupt),
        ):
            worker.run_openbench_worker(CLIENT_ARGS)

    def test_client_exits_130_on_interrupt(self):
        with tempfile.TemporaryDirectory() as workdir:
            shutil.copy(os.path.join(CLIENT_DIR, 'client.py'), workdir)
            with open(os.path.join(workdir, 'worker.py'), 'w') as fout:
                fout.write('def run_openbench_worker(args):\n    raise KeyboardInterrupt()\n')
            argv = [
                sys.executable,
                'client.py',
                '-U',
                'u',
                '-P',
                'p',
                '-S',
                'https://ob.invalid',
                '--no-client-downloads',
            ]
            result = subprocess.run(argv, cwd=workdir, capture_output=True)
        self.assertEqual(result.returncode, 130, result.stderr)

    def test_missing_tool_exits_nonzero(self):
        with (
            mock.patch.object(worker, 'get_version', side_effect=OSError),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            worker.locate_utility('make')
        self.assertEqual(raised.exception.code, 1)

    def test_single_workload_turns_sigterm_into_an_interrupt(self):
        config = config_with(None)
        with (
            mock.patch.object(worker, 'parse_arguments', return_value=argparse.Namespace(print_system_info=False)),
            mock.patch.object(worker, 'Configuration', return_value=config),
            mock.patch.object(worker, 'reload_local_imports'),
            mock.patch.object(worker, 'server_configure_fastchess', side_effect=KeyboardInterrupt),
            mock.patch.object(worker.signal, 'signal') as installed,
            self.assertRaises(KeyboardInterrupt),
        ):
            worker.run_openbench_worker(CLIENT_ARGS)
        installed.assert_called_once_with(worker.signal.SIGTERM, worker.raise_keyboard_interrupt)


class PrintSystemInfoTests(unittest.TestCase):
    def test_prints_the_registration_info_and_exits(self):
        config = argparse.Namespace(
            compilers={'Avalanche': ['zig', '0.16.0']},
            git_tokens={},
            cpu_flags=['AVX2'],
            cpu_name='cpu',
            isa_name='x86-64-avx2',
            os_name='Linux',
            os_ver='6',
            python_ver='3.14',
            mac_address='0',
            logical_cores=4,
            physical_cores=4,
            ram_total_mb=8192,
            machine_id=None,
            identity='lab-worker',
            threads=4,
            sockets=1,
            syzygy_max=2,
            noisy=False,
            focus=[],
            only=[],
            cli_options='',
            cxx_comp='g++',
            fastchess_ver=None,
            single=False,
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(worker, 'parse_arguments', return_value=argparse.Namespace(print_system_info=True)),
            mock.patch.object(worker, 'Configuration', return_value=config),
            mock.patch.object(worker, 'reload_local_imports'),
            mock.patch.object(worker, 'scan_system') as scanned,
            mock.patch.object(worker, 'server_configure_worker') as registered,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            worker.run_openbench_worker(CLIENT_ARGS)

        self.assertEqual(raised.exception.code, 0)
        scanned.assert_called_once_with(config)
        registered.assert_not_called()
        line = [x for x in stdout.getvalue().splitlines() if x.startswith('SYSTEM_INFO ')][0]
        info = json.loads(line.removeprefix('SYSTEM_INFO '))
        self.assertEqual(info['concurrency'], 4)
        self.assertEqual(info['compilers'], {'Avalanche': ['zig', '0.16.0']})
