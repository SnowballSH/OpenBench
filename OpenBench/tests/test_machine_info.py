import ast
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from OpenBench.insights.server import fleet_status
from OpenBench.machine_info import decode_system_info, int_of, malformed_fields, text_of
from OpenBench.models import Machine, Result
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    system_info,
)
from OpenBench.workloads.view_workload import fetch_result_summaries

CLIENT_WORKER = Path(settings.BASE_DIR) / 'Client' / 'worker.py'

CRAFTED: dict[str, Any] = {
    'concurrency': 'x',
    'physical_cores': '8',
    'cpu_name': ['a', 'b'],
    'isa_name': {'x': 1},
    'machine_name': 7,
    'cpu_flags': 'AVX2',
    'noisy': 1,
}


def client_registration_keys() -> set[str]:
    tree = ast.parse(CLIENT_WORKER.read_text())
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'registration_info'
    )
    literal = next(node for node in ast.walk(function) if isinstance(node, ast.Dict))
    return {key.value for key in literal.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}


def genuine_system_info() -> dict[str, Any]:
    # Every key Client/worker.py registration_info() sends, with the types it sends them as
    return system_info(
        cpu_name='AMD Ryzen 9 7950X 16-Core Processor',
        isa_name='x86-64-avx512',
        os_ver='6.8.0-45-generic',
        python_ver='3.14.0',
        mac_address='A1B2C3D4E5F6',
        machine_id=None,
        machine_name='None',
        focus=['Avalanche'],
        only=[],
        cli_options='-T 4 -N 1',
        cxx_comp='g++',
        fastchess_ver='1.4.0-alpha',
    )


class SystemInfoValidationTests(SimpleTestCase):
    def test_genuine_client_info_covers_every_client_key(self):
        self.assertEqual(set(genuine_system_info()), client_registration_keys())

    def test_genuine_client_info_is_well_formed(self):
        self.assertEqual(malformed_fields(genuine_system_info()), [])

    def test_core_counts_psutil_cannot_determine_are_accepted(self):
        info = {**genuine_system_info(), 'physical_cores': None, 'logical_cores': None}
        self.assertEqual(malformed_fields(json.loads(json.dumps(info))), [])

    def test_crafted_fields_are_named(self):
        self.assertEqual(
            sorted(malformed_fields({**system_info(), **CRAFTED})),
            sorted(CRAFTED),
        )

    def test_missing_and_mistyped_list_items(self):
        info = system_info(only=['Avalanche', 3])
        del info['tokens']
        self.assertEqual(malformed_fields(info), ['tokens', 'only'])

    def test_decoding_refuses_non_objects(self):
        for raw in (None, '', 'not json', '[1]', '"text"'):
            self.assertIsNone(decode_system_info(raw), raw)
        self.assertEqual(decode_system_info('{"a": 1}'), {'a': 1})

    def test_readers_coerce_anything(self):
        info = {'concurrency': 'x', 'huge': 1e999, 'cpu_name': ['a'], 'none': 'None'}
        self.assertEqual((int_of(info, 'concurrency'), int_of(info, 'huge'), int_of([], 'concurrency')), (0, 0, 0))
        texts = (text_of(info, 'cpu_name'), text_of(info, 'none'), text_of('text', 'x'))
        self.assertEqual(texts, ("['a']", None, None))
        self.assertEqual(fleet_status([({'concurrency': 'x'}, 1.0), ({'concurrency': 4}, 1.0)]).threads, 4)


class RegistrationTests(TestCase):
    def setUp(self):
        create_engine_config()
        self.worker = create_user('lab-worker')

    def register(self, info: object) -> dict[str, Any]:
        payload = {**credentials(self.worker), 'system_info': json.dumps(info)}
        response = self.client.post('/clientWorkerInfo/', payload)
        self.assertEqual(response.status_code, 200)
        body: dict[str, Any] = response.json()
        return body

    def test_genuine_client_registers(self):
        response = self.register(genuine_system_info())
        self.assertEqual(set(response), {'machine_id', 'secret'})
        self.assertEqual(Machine.objects.get().info['supported'], ['Avalanche'])

    def test_crafted_info_is_refused_like_any_registration_error(self):
        for key, value in CRAFTED.items():
            response = self.register({**system_info(), key: value})
            self.assertEqual(response, {'error': f'Malformed system_info: {key}'}, key)
        self.assertFalse(Machine.objects.exists())

    def test_undecodable_info_is_refused(self):
        for raw in ('not json', '[]'):
            response = self.client.post('/clientWorkerInfo/', {**credentials(self.worker), 'system_info': raw})
            self.assertEqual(response.json(), {'error': 'Malformed system_info'}, raw)
        missing = self.client.post('/clientWorkerInfo/', credentials(self.worker))
        self.assertEqual(missing.json(), {'error': 'Malformed system_info'})

    def test_old_clients_still_hear_about_their_version_first(self):
        response = self.register({**system_info(), 'client_ver': 1, 'concurrency': 'x'})
        self.assertIn('Bad Client Version', response['error'])


class StoredCraftedInfoTests(TestCase):
    # Machines stored before registration was validated must not break pages for everyone
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.reader = create_user('reader')
        self.test = create_test(self.reader)
        machine = Machine.objects.create(user=self.reader, info={**system_info(), **CRAFTED})
        Machine.objects.filter(id=machine.id).update(updated=timezone.now() - timedelta(seconds=5))
        Result.objects.create(test=self.test, machine=machine, LL=1, DD=2, WW=1)
        self.client.force_login(self.reader)

    def test_server_insights(self):
        self.assertEqual(self.client.get('/api/insights/server/').status_code, 200)

    def test_workload_insights_and_summary(self):
        for query in ('insights', 'summary'):
            url = f'/api/workload/{self.test.id}/{query}/'
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_result_summaries_group_crafted_names_as_text(self):
        [row] = fetch_result_summaries(self.test)['cpu_name']
        self.assertEqual(row['key'], "['a', 'b']")
