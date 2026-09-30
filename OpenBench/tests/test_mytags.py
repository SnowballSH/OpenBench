from django.test import TestCase

from OpenBench.models import Network
from OpenBench.templatetags.mytags import (
    longStatBlock,
    prettyDevName,
    shortStatBlock,
    test_is_smp_odds,
    testResultColour,
)
from OpenBench.tests.fixtures import create_engine_config, create_test, create_user, ensure_book


class PrettyDevNameTests(TestCase):
    def setUp(self):
        create_engine_config()
        create_engine_config('Other')
        ensure_book()
        self.author = create_user('author')

    def network_test(self, **fields):
        test = create_test(self.author, dev_network='AAAAAAAA', base_network='BBBBBBBB', dev_netname='mine', **fields)
        test.base.name = test.dev.name
        return test

    def test_uses_the_network_name(self):
        Network.objects.create(sha256='AAAAAAAA', name='renamed', engine='Avalanche', author='author')
        self.assertEqual(prettyDevName(self.network_test()), 'renamed')

    def test_ignores_another_engines_network_with_the_same_sha(self):
        Network.objects.create(sha256='AAAAAAAA', name='theirs', engine='Other', author='author')
        self.assertEqual(prettyDevName(self.network_test()), 'mine')

    def test_different_engines_show_the_base(self):
        test = create_test(self.author, base_engine='Other')
        self.assertEqual(prettyDevName(test), '[Other] base')


class StatBlockTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')

    def test_sprt_block(self):
        test = create_test(
            self.author,
            test_mode='SPRT',
            currentllr=1.234,
            LL=1,
            LD=2,
            DD=3,
            DW=4,
            WW=5,
            wins=7,
            losses=3,
            draws=10,
            games=20,
        )
        self.assertEqual(
            shortStatBlock(test).split('\n'),
            ['LLR: 1.23 (-2.94, 2.94) [0.00, 3.00]', 'Games: 20 W: 7 L: 3 D: 10', 'Ptnml(0-2): 1, 2, 3, 4, 5'],
        )

    def test_colours(self):
        self.assertEqual(testResultColour(create_test(self.author, passed=True)), 'green')
        self.assertEqual(testResultColour(create_test(self.author, passed=True, elolower=-3.0, eloupper=1.0)), 'blue')
        self.assertEqual(testResultColour(create_test(self.author, failed=True, wins=5, losses=5)), 'yellow')
        self.assertEqual(testResultColour(create_test(self.author, failed=True, wins=4, losses=5)), 'red')
        self.assertEqual(testResultColour(create_test(self.author)), '')


class MissingOptionTests(TestCase):
    def setUp(self):
        create_engine_config()
        ensure_book()
        self.author = create_user('author')

    def settings_line(self, dev_options):
        test = create_test(self.author, test_mode='SPRT', dev_options=dev_options, dev_time_control='8.0+0.08')
        return longStatBlock(test).split('\n')[1]

    def test_long_block_shows_the_options_it_can_read(self):
        self.assertEqual(self.settings_line('Threads=2 Hash=16'), 'SPRT  | 8.0+0.08s Threads=2 Hash=16MB')
        self.assertEqual(self.settings_line('Threads=2'), 'SPRT  | 8.0+0.08s Threads=2')
        self.assertEqual(self.settings_line('Hash=abc Threads=x'), 'SPRT  | 8.0+0.08s')
        self.assertEqual(self.settings_line(''), 'SPRT  | 8.0+0.08s')

    def test_thread_odds_tolerate_missing_threads(self):
        def odds(dev, base):
            return test_is_smp_odds(create_test(self.author, dev_options=dev, base_options=base))

        self.assertFalse(odds('Hash=16', 'Hash=16'))
        self.assertTrue(odds('Threads=2', 'Hash=16'))
        self.assertFalse(odds('Threads=2', 'Threads=2 Hash=8'))
        self.assertTrue(odds('Threads=2', 'Threads=1'))
