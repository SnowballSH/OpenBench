import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings

from OpenBench.models import Engine, EngineConfig, Network, Test
from OpenBench.spsa_utils import create_spsa_run
from OpenBench.tests.fixtures import (
    PASSWORD,
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
)
from OpenBench.tests.test_create_workload import (
    github_commit,
    rendered_error,
    tune_fields,
)
from OpenBench.workloads.clone import (
    FORM_FIELDS,
    CloneError,
    clone_fields,
    load_clone_source,
    workload_type_of,
)

REPO = 'https://github.com/SnowballSH/Avalanche'
SPSA_INPUTS = 'Knight, int, 300, 200, 400, 10, 0.002\nBishop, float, 3.5, 3.0, 4.0, 0.1, 0.002'

GENERAL = {
    'book_name': 'UHO_Lichess_4852_v1.epd',
    'upload_pgns': 'FALSE',
    'info': 'Tweak LMR',
    'priority': '0',
    'throughput': '1000',
    'syzygy_wdl': 'OPTIONAL',
    'syzygy_adj': 'OPTIONAL',
    'win_adj': 'movecount=3 score=400',
    'draw_adj': 'movenumber=40 movecount=8 score=10',
    'scale_method': 'BASE',
    'scale_nps': '1000000',
}


def seeded_test(author, **fields):
    return create_test(author, **{'scale_nps': 1000000, **fields})


def engine_side(side, branch, network=''):
    return {
        f'{side}_engine': 'Avalanche',
        f'{side}_repo': REPO,
        f'{side}_branch': branch,
        f'{side}_bench': '',
        f'{side}_network': network,
        f'{side}_options': 'Threads=1 Hash=16',
        f'{side}_time_control': '8.0+0.08',
    }


def create_tune(author):
    dev = Engine.objects.create(name='tune-branch', source=REPO, sha='d' * 40, bench=1)
    tune = seeded_test(author, test_mode='SPSA', info='Tune pruning', workload_size=8)
    tune.dev = tune.base = dev
    tune.save()
    fields = tune_fields(
        spsa_inputs=SPSA_INPUTS,
        spsa_reporting_type='BULK',
        spsa_distribution_type='MULTIPLE',
        spsa_alpha='0.602',
        spsa_gamma='0.101',
        spsa_A_ratio='0.1',
        spsa_iterations='1000',
        spsa_pairs_per='8',
    )
    create_spsa_run(tune, SimpleNamespace(POST=fields)).save()
    return tune


def create_datagen(author):
    return seeded_test(
        author,
        test_mode='DATAGEN',
        info='Generate data',
        max_games=100000,
        genfens_args='-randmoves 4',
        play_reverses=True,
        upload_pgns='COMPACT',
        book_name='NONE',
    )


class FormControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = {}
        self.form_ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ('input', 'select', 'textarea') and 'name' in attributes:
            self.controls[attributes['name']] = attributes.get('value')
        if tag == 'form':
            self.form_ids.append(attributes.get('id'))


def form_controls(response):
    parser = FormControls()
    parser.feed(response.content.decode())
    return parser.controls


def form_ids(response):
    parser = FormControls()
    parser.feed(response.content.decode())
    return parser.form_ids


def prefill_payload(response):
    match = re.search(
        r'<script id="?json-prefill"? type="?application/json"?>(.*?)</script>',
        response.content.decode(),
        re.DOTALL,
    )
    return json.loads(match.group(1))


class CloneFieldsTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')

    def test_sprt_test_maps_every_form_field(self):
        test = seeded_test(self.author, info='Tweak LMR')
        self.assertEqual(
            clone_fields(test),
            {
                **engine_side('dev', 'dev'),
                **engine_side('base', 'base'),
                **GENERAL,
                'info': '',
                'workload_size': '32',
                'test_mode': 'SPRT',
                'test_bounds': '[0.0, 3.0]',
                'test_confidence': '[0.05, 0.05]',
                'test_max_games': 'N/A',
            },
        )

    def test_sprt_values_never_use_exponents(self):
        test = seeded_test(self.author, elolower=-0.5, eloupper=2.25, alpha=1e-05, beta=0.1)
        fields = clone_fields(test)
        self.assertEqual(fields['test_bounds'], '[-0.5, 2.25]')
        self.assertEqual(fields['test_confidence'], '[0.1, 0.00001]')

    def test_games_test_maps_max_games(self):
        test = seeded_test(self.author, test_mode='GAMES', max_games=4000)
        fields = clone_fields(test)
        self.assertEqual(
            {
                name: fields[name]
                for name in (
                    'test_mode',
                    'test_bounds',
                    'test_confidence',
                    'test_max_games',
                )
            },
            {
                'test_mode': 'GAMES',
                'test_bounds': 'N/A',
                'test_confidence': 'N/A',
                'test_max_games': '4000',
            },
        )

    def test_branch_bench_is_re_resolved_but_a_pinned_commit_keeps_its_bench(self):
        test = seeded_test(self.author)
        test.dev = Engine.objects.create(name='A' * 40, source=REPO, sha='a' * 40, bench=7654321)
        self.assertEqual(clone_fields(test)['dev_bench'], '7654321')
        self.assertEqual(clone_fields(test)['base_bench'], '')

    def test_test_info_is_kept_only_for_a_pinned_commit(self):
        test = seeded_test(self.author, info='Old commit message')
        self.assertEqual(clone_fields(test)['info'], '')
        test.dev = Engine.objects.create(name='a' * 40, source=REPO, sha='a' * 40)
        self.assertEqual(clone_fields(test)['info'], 'Old commit message')

    def test_an_unset_scale_nps_is_left_to_the_engine_preset(self):
        self.assertNotIn('scale_nps', clone_fields(create_test(self.author)))

    def test_networks_are_selected_by_sha(self):
        test = seeded_test(self.author, dev_network='AAAAAAAA', dev_netname='nezha')
        self.assertEqual(clone_fields(test)['dev_network'], 'AAAAAAAA')

    def test_tune_maps_the_spsa_inputs_and_settings(self):
        fields = clone_fields(create_tune(self.author))
        self.assertEqual(
            fields,
            {
                **engine_side('dev', 'tune-branch'),
                **GENERAL,
                'info': 'Tune pruning',
                'spsa_inputs': 'Knight, int, 300.0, 200.0, 400.0, 10.0, 0.002\n'
                'Bishop, float, 3.5, 3.0, 4.0, 0.1, 0.002',
                'spsa_reporting_type': 'BULK',
                'spsa_distribution_type': 'MULTIPLE',
                'spsa_alpha': '0.602',
                'spsa_gamma': '0.101',
                'spsa_A_ratio': '0.1',
                'spsa_iterations': '1000',
                'spsa_pairs_per': '8',
            },
        )

    def test_datagen_maps_its_settings(self):
        fields = clone_fields(create_datagen(self.author))
        self.assertEqual(
            {
                name: fields[name]
                for name in (
                    'datagen_max_games',
                    'datagen_custom_genfens',
                    'datagen_play_reverses',
                )
            },
            {
                'datagen_max_games': '100000',
                'datagen_custom_genfens': '-randmoves 4',
                'datagen_play_reverses': 'YES',
            },
        )
        self.assertEqual((fields['book_name'], fields['upload_pgns']), ('NONE', 'COMPACT'))

    def test_every_type_maps_exactly_its_form_fields(self):
        for workload in (
            seeded_test(self.author),
            create_tune(self.author),
            create_datagen(self.author),
        ):
            self.assertEqual(
                set(clone_fields(workload)),
                set(FORM_FIELDS[workload_type_of(workload)]),
            )


class CloneRoundTripTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        Network.objects.create(sha256='AAAAAAAA', name='nezha', engine='Avalanche', author='author')
        self.author = create_user('author')
        self.client.login(username='author', password=PASSWORD)

    def resubmit(self, workload):
        kind = workload.workload_type_str()
        with mock.patch('requests.get', side_effect=github_commit):
            response = self.client.post(f'/{kind}/new/', clone_fields(workload))
        self.assertEqual(response.status_code, 302, rendered_error(response))
        return Test.objects.latest('id')

    def test_resubmitting_a_clone_recreates_an_identical_workload(self):
        workloads = [
            seeded_test(
                self.author,
                info='Tweak',
                dev_network='AAAAAAAA',
                elolower=-3.0,
                eloupper=0.5,
            ),
            seeded_test(
                self.author,
                info='Scaling',
                test_mode='GAMES',
                max_games=4000,
                threads=4,
            ),
            create_tune(self.author),
            create_datagen(self.author),
        ]
        for workload in workloads:
            with self.subTest(workload.test_mode):
                self.assertEqual(clone_fields(self.resubmit(workload)), clone_fields(workload))

    @override_settings(DEBUG=True)
    def test_every_seeded_workload_clones_into_a_valid_submission(self):
        from OpenBench.workloads.verify_workload import verify_workload

        Test.objects.all().delete()
        call_command('seed_demo', stdout=io.StringIO())
        Network.objects.create(sha256='BCF481FD', name='nezha', engine='Avalanche', author='admin')

        engine_preset = {'scale_nps': str(EngineConfig.objects.get().nps)}

        for workload in Test.objects.all():
            kind = workload_type_of(workload)
            fields = engine_preset | clone_fields(workload)
            request = RequestFactory().post(f'/{kind.lower()}/new/', fields)
            with (
                mock.patch('requests.get', side_effect=github_commit),
                self.subTest(workload.dev.name),
            ):
                self.assertEqual(verify_workload(request, kind)[0], [])


class ClonePageTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.client.login(username='author', password=PASSWORD)
        self.test = seeded_test(self.author, info='Tweak LMR')

    def warning(self, response):
        return 'Nothing was cloned' in response.content.decode()

    def test_the_create_page_carries_the_payload_and_notice(self):
        response = self.client.get(f'/test/new/?clone={self.test.id}')
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(prefill_payload(response), clone_fields(self.test))
        self.assertIn(f'#{self.test.id} dev</a>', content)
        self.assertIn(f'/test/{self.test.id}/', content)
        self.assertEqual(form_controls(response)['clone_of'], str(self.test.id))
        self.assertFalse(self.warning(response))

    def test_a_plain_create_page_has_no_payload(self):
        response = self.client.get('/test/new/')
        self.assertIsNone(prefill_payload(response))
        self.assertNotIn('Cloned from', response.content.decode())

    def test_payload_cannot_break_out_of_its_script(self):
        self.test.dev_options = '</script><script>alert(1)</script>'
        self.test.save()
        response = self.client.get(f'/test/new/?clone={self.test.id}')
        self.assertNotIn('<script>alert(1)', response.content.decode())
        self.assertEqual(prefill_payload(response)['dev_options'], self.test.dev_options)

    def test_invalid_ids_are_ignored_with_a_warning(self):
        for raw in ['abc', '', '-1', '0', '999999', str(10**30), '1e3']:
            with self.subTest(raw):
                response = self.client.get('/test/new/', {'clone': raw})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(prefill_payload(response))
                self.assertTrue(self.warning(response))

    def test_a_workload_of_another_type_is_not_cloned(self):
        tune = create_tune(self.author)
        response = self.client.get(f'/test/new/?clone={tune.id}')
        self.assertIsNone(prefill_payload(response))
        self.assertIn('is a tune, not a test', response.content.decode())

    def test_a_tune_without_its_spsa_run_is_not_cloned(self):
        orphan = create_test(self.author, test_mode='SPSA')
        with self.assertRaises(CloneError):
            load_clone_source(str(orphan.id), 'TUNE')

    def test_every_form_field_exists_on_its_create_page(self):
        for kind, names in FORM_FIELDS.items():
            controls = form_controls(self.client.get(f'/{kind.lower()}/new/'))
            self.assertLessEqual(set(names), set(controls), kind)

    def test_a_rejected_submission_re_renders_with_its_values_and_notice(self):
        fields = {
            **clone_fields(self.test),
            'dev_time_control': '60.0+0.6',
            'throughput': '-5',
        }
        with mock.patch('requests.get', side_effect=github_commit):
            response = self.client.post('/test/new/', {**fields, 'clone_of': str(self.test.id)})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Throughput', rendered_error(response))
        self.assertEqual(prefill_payload(response), fields)
        self.assertIn(f'#{self.test.id} dev</a>', response.content.decode())
        self.assertEqual(Test.objects.count(), 1)

    def test_unrestored_fields_are_reported_above_the_create_form(self):
        response = self.client.get(f'/test/new/?clone={self.test.id}')
        self.assertEqual(form_ids(response).count('workload-form'), 1)
        script = (Path(settings.BASE_DIR) / 'OpenBench/static/create_workload.js').read_text()
        self.assertIn("getElementById('workload-form')", script)
        self.assertNotIn("querySelector('form')", script)

    def test_a_disabled_user_sees_a_disabled_clone_button(self):
        create_user('reader', enabled=False)
        self.client.login(username='reader', password=PASSWORD)
        content = self.client.get(f'/test/{self.test.id}/').content.decode()
        self.assertNotIn(f'?clone={self.test.id}', content)
        self.assertRegex(content, r'<a class="anchorbutton btn-disabled">Clone</a>')

    def test_the_workload_page_links_to_clone(self):
        for workload in (
            self.test,
            create_tune(self.author),
            create_datagen(self.author),
        ):
            kind = workload.workload_type_str()
            response = self.client.get(f'/{kind}/{workload.id}/')
            self.assertIn(f'/{kind}/new/?clone={workload.id}', response.content.decode())
