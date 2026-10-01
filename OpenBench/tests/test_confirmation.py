import json
import re
from typing import Any

from django.contrib.auth.models import User
from django.test import TestCase

from OpenBench.models import Engine, EngineConfig, Profile, Test
from OpenBench.progress.domain import TimeClass
from OpenBench.tests.fixtures import (
    LTC_PRESET,
    STC_PRESET,
    create_engine_config,
    create_test,
    create_user,
    ensure_book,
    present,
    set_test_presets,
)
from OpenBench.workloads.clone import CloneError, load_clone_source
from OpenBench.workloads.confirmation import Confirmation, ExistingConfirmation, confirmation_for
from OpenBench.workloads.presets import (
    IDENTITY_FIELDS,
    expanded,
    preset_of_class,
    preset_time_class,
    run_settings,
    test_preset,
)

DEFAULT = {
    'both_branch': 'master',
    'both_bench': '123',
    'book_name': 'UHO_Lichess_4852_v1.epd',
    'test_bounds': '[0.00, 3.00]',
    'test_confidence': '[0.05, 0.05]',
    'test_max_games': 40000,
    'workload_size': 32,
}
SMP = {'both_options': 'Threads=4 Hash=64', 'both_time_control': '8.0+0.08'}
PREFILL = re.compile(r'<script id="json-prefill" type="application/json">(.*?)</script>', re.DOTALL)


def set_presets(config: EngineConfig, **presets: dict[str, Any]) -> EngineConfig:
    return set_test_presets(config, DEFAULT, **presets)


def passed_stc(author: User, **fields: Any) -> Test:
    return create_test(author, **{'finished': True, 'passed': True, **fields})


def profile_of(user: User) -> Profile:
    return Profile.objects.get(user=user)


class PresetTests(TestCase):
    def setUp(self) -> None:
        self.config = set_presets(create_engine_config(), STC=STC_PRESET, LTC=LTC_PRESET, SMP=SMP)

    def test_a_shared_key_fills_both_sides_and_a_side_key_outranks_it(self) -> None:
        preset = expanded({'dev_options': 'Threads=2 Hash=8', 'both_options': 'Threads=1 Hash=8', 'priority': 3})

        self.assertEqual(
            preset, {'dev_options': 'Threads=2 Hash=8', 'base_options': 'Threads=1 Hash=8', 'priority': '3'}
        )

    def test_a_named_preset_inherits_the_default(self) -> None:
        preset = present(test_preset(self.config, 'LTC'))

        self.assertEqual(preset['dev_time_control'], '40.0+0.40')
        self.assertEqual(preset['base_options'], 'Threads=1 Hash=64')
        self.assertEqual(preset['workload_size'], '8')
        self.assertEqual(preset['test_bounds'], '[0.00, 3.00]')

    def test_the_default_and_unknown_names_are_not_presets(self) -> None:
        self.assertIsNone(test_preset(self.config, 'default'))
        self.assertIsNone(test_preset(self.config, 'VLTC'))

    def test_presets_are_classed_like_workloads(self) -> None:
        classes = {name: preset_time_class(present(test_preset(self.config, name))) for name in ('STC', 'LTC', 'SMP')}

        self.assertEqual(classes, {'STC': TimeClass.STC, 'LTC': TimeClass.LTC, 'SMP': TimeClass.SMP})

    def test_the_long_preset_is_found_by_its_time_control_whatever_its_name(self) -> None:
        self.assertEqual(preset_of_class(self.config, TimeClass.LTC), 'LTC')

        set_presets(self.config, STC=STC_PRESET, Long=LTC_PRESET, LTC={**LTC_PRESET, 'both_time_control': '60.0+0.60'})
        self.assertEqual(preset_of_class(self.config, TimeClass.LTC), 'LTC')

        set_presets(self.config, STC=STC_PRESET, Long=LTC_PRESET)
        self.assertEqual(preset_of_class(self.config, TimeClass.LTC), 'Long')

        set_presets(self.config, STC=STC_PRESET)
        self.assertIsNone(preset_of_class(self.config, TimeClass.LTC))

    def test_booleans_become_the_forms_words(self) -> None:
        self.assertEqual(
            expanded({'upload_pgns': True, 'both_bench': 12}),
            {
                'upload_pgns': 'TRUE',
                'dev_bench': '12',
                'base_bench': '12',
            },
        )

    def test_presets_that_are_not_objects_are_passed_over(self) -> None:
        self.config.presets = {'test_presets': {'default': DEFAULT, 'LTC': LTC_PRESET, 'broken': 'x'}}
        self.assertEqual(preset_of_class(self.config, TimeClass.LTC), 'LTC')
        self.assertIsNone(test_preset(self.config, 'broken'))

        shapes: tuple[Any, ...] = ({'test_presets': ['LTC']}, {'test_presets': None}, {}, [])
        for broken in shapes:
            self.config.presets = broken
            self.assertIsNone(preset_of_class(self.config, TimeClass.LTC))
            self.assertIsNone(test_preset(self.config, 'LTC'))

    def test_identity_fields_cover_everything_the_clone_keeps(self) -> None:
        kept = {
            f'{side}_{field}' for side in ('dev', 'base') for field in ('engine', 'repo', 'branch', 'bench', 'network')
        }
        self.assertEqual(IDENTITY_FIELDS, kept | {'info'})

    def test_run_settings_leave_out_what_identifies_the_engines(self) -> None:
        settings = run_settings(present(test_preset(self.config, 'LTC')))

        self.assertFalse({'dev_branch', 'base_branch', 'dev_bench', 'base_bench'} & set(settings))
        self.assertEqual(settings['dev_time_control'], '40.0+0.40')


class ConfirmationTests(TestCase):
    def setUp(self) -> None:
        self.config = set_presets(create_engine_config(), STC=STC_PRESET, LTC=LTC_PRESET)
        ensure_book()
        self.author = create_user('author')

    def confirmed(self, test: Test, user: User | None = None) -> bool:
        return confirmation_for(test, profile_of(user or self.author)) is not None

    def test_a_passed_stc_sprt_is_offered_the_long_preset(self) -> None:
        test = passed_stc(self.author)

        self.assertEqual(
            confirmation_for(test, profile_of(self.author)),
            Confirmation(preset='LTC', time_control='40.0+0.40', url=f'/test/new/?clone={test.id}&preset=LTC'),
        )

    def test_only_a_finished_passed_sprt_is_offered_it(self) -> None:
        refused = {
            'running': create_test(self.author),
            'failed': create_test(self.author, finished=True, failed=True),
            'stopped': create_test(self.author, finished=True),
            'deleted': passed_stc(self.author, deleted=True),
            'fixed games': passed_stc(self.author, test_mode='GAMES', max_games=1000),
            'datagen': passed_stc(self.author, test_mode='DATAGEN'),
        }

        for name, test in refused.items():
            with self.subTest(name):
                self.assertFalse(self.confirmed(test))

    def test_a_test_that_is_not_short_time_control_is_not_offered_it(self) -> None:
        refused = {
            'already LTC': passed_stc(self.author, dev_time_control='40.0+0.40', base_time_control='40.0+0.40'),
            'SMP': passed_stc(self.author, threads=4),
            'time odds': passed_stc(self.author, base_time_control='16.0+0.16'),
            'cross engine': passed_stc(self.author, base_engine='Other'),
        }

        for name, test in refused.items():
            with self.subTest(name):
                self.assertFalse(self.confirmed(test))

    def test_it_needs_a_long_preset_on_an_enabled_engine(self) -> None:
        test = passed_stc(self.author)

        set_presets(self.config, STC=STC_PRESET)
        self.assertFalse(self.confirmed(test))

        set_presets(self.config, STC=STC_PRESET, LTC=LTC_PRESET)
        EngineConfig.objects.update(enabled=False)
        self.assertFalse(self.confirmed(test))

    def ltc_run(self, test: Test, **fields: Any) -> Test:
        run = create_test(self.author, dev_time_control='40.0+0.40', base_time_control='40.0+0.40', **fields)
        Engine.objects.filter(id=run.dev_id).update(sha=test.dev.sha)
        Engine.objects.filter(id=run.base_id).update(sha=test.base.sha)
        return run

    def test_a_pair_already_at_ltc_links_to_that_run_instead(self) -> None:
        test = passed_stc(self.author)
        self.ltc_run(test, finished=True, failed=True)
        running = self.ltc_run(test)

        for viewer in (profile_of(self.author), None):
            self.assertEqual(
                confirmation_for(test, viewer), ExistingConfirmation(running.id, '40.0+0.40', f'/test/{running.id}/')
            )

    def test_other_runs_of_the_pair_do_not_count(self) -> None:
        test = passed_stc(self.author)
        self.ltc_run(test, deleted=True)
        self.ltc_run(test, test_mode='GAMES', max_games=1000)
        self.ltc_run(test, dev_network='ABCDEF01')
        another_dev = self.ltc_run(test)
        Engine.objects.filter(id=another_dev.dev_id).update(sha='c' * 40)

        self.assertIsInstance(confirmation_for(test, profile_of(self.author)), Confirmation)

    def test_it_needs_an_account_that_may_create_tests(self) -> None:
        test = passed_stc(self.author)

        self.assertFalse(self.confirmed(test, create_user('disabled', enabled=False)))
        self.assertIsNone(confirmation_for(test, None))


class PresetCloneTests(TestCase):
    def setUp(self) -> None:
        self.config = set_presets(create_engine_config(), STC=STC_PRESET, LTC=LTC_PRESET)
        ensure_book()
        self.author = create_user('author')
        self.client.force_login(self.author)

    def test_the_preset_replaces_how_the_test_runs_and_keeps_what_it_tests(self) -> None:
        test = passed_stc(self.author, elolower=-3.0, eloupper=1.0)
        plain = load_clone_source(str(test.id), 'TEST').fields

        source = load_clone_source(str(test.id), 'TEST', 'LTC')

        changed = {name: value for name, value in source.fields.items() if plain[name] != value}
        self.assertEqual(source.preset, 'LTC')
        self.assertEqual(
            changed,
            {
                'dev_time_control': '40.0+0.40',
                'base_time_control': '40.0+0.40',
                'dev_options': 'Threads=1 Hash=64',
                'base_options': 'Threads=1 Hash=64',
                'workload_size': '8',
                'priority': '2',
                'test_bounds': '[0.00, 3.00]',
            },
        )
        self.assertEqual((source.fields['dev_branch'], source.fields['base_branch']), ('dev', 'base'))
        self.assertEqual(source.fields['test_max_games'], 'N/A')
        self.assertEqual(source.preset_changes, ('time control', 'options', 'SPRT bounds', 'priority', 'workload size'))

    def test_equal_numbers_written_differently_are_not_a_change(self) -> None:
        test = passed_stc(self.author)

        source = load_clone_source(str(test.id), 'TEST', 'LTC')

        self.assertEqual(source.fields['test_bounds'], '[0.00, 3.00]')
        self.assertNotIn('SPRT bounds', source.preset_changes)
        self.assertNotIn('book', source.preset_changes)

    def test_a_preset_that_moves_one_side_names_it(self) -> None:
        set_presets(self.config, STC={**STC_PRESET, 'base_options': 'Threads=1 Hash=128'})
        test = passed_stc(self.author)

        self.assertEqual(load_clone_source(str(test.id), 'TEST', 'STC').preset_changes, ('base options',))

    def test_a_preset_that_changes_nothing_says_so(self) -> None:
        test = passed_stc(self.author)

        content = self.client.get(f'/test/new/?clone={test.id}&preset=STC').content.decode()

        self.assertIn('the STC preset changes nothing', content)

    def test_a_plain_clone_carries_no_preset(self) -> None:
        test = passed_stc(self.author)

        self.assertIsNone(load_clone_source(str(test.id), 'TEST').preset)
        self.assertIsNone(load_clone_source(str(test.id), 'TEST', '').preset)

    def test_a_fixed_games_clone_takes_the_presets_length_not_its_bounds(self) -> None:
        test = passed_stc(self.author, test_mode='GAMES', max_games=1000)

        fields = load_clone_source(str(test.id), 'TEST', 'LTC').fields

        self.assertEqual(fields['test_max_games'], '40000')
        self.assertEqual((fields['test_bounds'], fields['test_confidence']), ('N/A', 'N/A'))

    def test_an_unknown_preset_clones_nothing(self) -> None:
        test = passed_stc(self.author)

        for name in ('VLTC', 'default', 'x' * 65):
            with self.subTest(name), self.assertRaisesMessage(CloneError, 'has no test preset with that name'):
                load_clone_source(str(test.id), 'TEST', name)

    def test_only_a_test_takes_a_preset(self) -> None:
        datagen = passed_stc(self.author, test_mode='DATAGEN')

        with self.assertRaisesMessage(CloneError, 'only a test can be cloned with a preset'):
            load_clone_source(str(datagen.id), 'DATAGEN', 'LTC')

    def test_the_create_form_is_prefilled_and_says_which_preset(self) -> None:
        test = passed_stc(self.author)

        content = self.client.get(f'/test/new/?clone={test.id}&preset=LTC').content.decode()

        prefill = json.loads(present(PREFILL.search(content)).group(1))
        self.assertEqual(prefill['dev_time_control'], '40.0+0.40')
        self.assertEqual(prefill['dev_branch'], 'dev')
        self.assertIn('with the LTC preset in place of its time control, options, priority, workload size<', content)
        self.assertFalse(Test.objects.exclude(id=test.id).exists())

    def test_an_unknown_preset_warns_and_opens_a_blank_form(self) -> None:
        test = passed_stc(self.author)

        content = self.client.get(f'/test/new/?clone={test.id}&preset=VLTC').content.decode()

        self.assertIn('Nothing was cloned: Avalanche has no test preset with that name', content)
        self.assertEqual(json.loads(present(PREFILL.search(content)).group(1)), None)


class ConfirmButtonTests(TestCase):
    def setUp(self) -> None:
        set_presets(create_engine_config(), STC=STC_PRESET, LTC=LTC_PRESET)
        ensure_book()
        self.author = create_user('author')
        self.client.force_login(self.author)

    def page(self, test: Test) -> str:
        return self.client.get(f'/test/{test.id}/').content.decode()

    def test_a_passed_stc_test_links_to_the_prefilled_form(self) -> None:
        test = passed_stc(self.author)

        content = self.page(test)

        self.assertIn(f'href="/test/new/?clone={test.id}&amp;preset=LTC"', content)
        self.assertIn('Confirm at LTC (40.0+0.40)</a>', content)

    def test_a_confirmed_pair_links_to_its_ltc_run(self) -> None:
        test = passed_stc(self.author)
        run = create_test(self.author, dev_time_control='40.0+0.40', base_time_control='40.0+0.40')
        Engine.objects.filter(id=run.dev_id).update(sha=test.dev.sha)
        Engine.objects.filter(id=run.base_id).update(sha=test.base.sha)

        content = self.page(test)

        self.assertNotIn('Confirm at LTC', content)
        self.assertIn(f'href="/test/{run.id}/">LTC confirmation: #{run.id} (40.0+0.40)</a>', content)

    def test_other_tests_do_not(self) -> None:
        for test in (create_test(self.author), create_test(self.author, finished=True, failed=True)):
            self.assertNotIn('Confirm at LTC', self.page(test))

    def test_a_disabled_account_is_not_offered_it(self) -> None:
        test = passed_stc(self.author)
        self.client.force_login(create_user('disabled', enabled=False))

        self.assertNotIn('Confirm at LTC', self.page(test))
