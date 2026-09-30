from types import SimpleNamespace

from django.test import SimpleTestCase

from OpenBench.utils import TimeControl, workload_uses_time_based_tc

class ParseTests(SimpleTestCase):

    def assertParses(self, text, expected):
        self.assertEqual(TimeControl.parse(text), expected, text)

    def assertRejected(self, text):
        with self.assertRaises(ValueError, msg=text):
            TimeControl.parse(text)

    def test_fixed_short_forms(self):
        self.assertParses('N=25000', 'N=25000')
        self.assertParses('n=25000', 'N=25000')
        self.assertParses('D=12', 'D=12')
        self.assertParses('MT=250', 'MT=250')

    def test_fixed_long_forms(self):
        self.assertParses('nodes=25000', 'N=25000')
        self.assertParses('depth=12', 'D=12')
        self.assertParses('movetime=250', 'MT=250')
        self.assertParses('NODES=25000', 'N=25000')

    def test_fischer(self):
        self.assertParses('8+0.08', '8.0+0.08')
        self.assertParses('8.0+0.08', '8.0+0.08')
        self.assertParses('60', '60.0+0.00')
        self.assertParses(' 10+0.1 ', '10.0+0.10')

    def test_increment_without_leading_digit_is_kept(self):
        self.assertParses('10+.1', '10.0+0.10')
        self.assertParses('.5+.05', '0.5+0.05')

    def test_cyclic(self):
        self.assertParses('40/60+0.6', '40/60.0+0.60')
        self.assertParses('40/60', '40/60.0+0.00')

    def test_reparsing_is_stable(self):
        for text in ['N=25000', '8.0+0.08', '40/60.0+0.60', 'D=12', 'MT=250']:
            self.assertParses(text, text)

    def test_garbage_is_rejected(self):
        for text in ['', 'abc', 'N=', 'nodes=abc', '5+0.05 junk', '8.0 + 0.08', '+0.1', '8+0.1+0.1', 'X=100']:
            self.assertRejected(text)

    def test_zero_length_controls_are_rejected(self):
        for text in ['0', '0+0', '0/10+1', '40/0+1', 'N=0', 'D=0', 'MT=0']:
            self.assertRejected(text)

    def test_controls_that_round_to_zero_are_rejected(self):
        for text in ['0.01+0.001', '0.04', '40/0.04+1']:
            self.assertRejected(text)
        self.assertParses('0.05+0.005', '0.1+0.01')

class ControlTypeTests(SimpleTestCase):

    def test_types(self):
        self.assertEqual(TimeControl.control_type('N=25000'), TimeControl.FIXED_NODES)
        self.assertEqual(TimeControl.control_type('D=12'), TimeControl.FIXED_DEPTH)
        self.assertEqual(TimeControl.control_type('MT=250'), TimeControl.FIXED_TIME)
        self.assertEqual(TimeControl.control_type('40/60.0+0.60'), TimeControl.CYCLIC)
        self.assertEqual(TimeControl.control_type('8.0+0.08'), TimeControl.FISCHER)

    def test_base(self):
        self.assertEqual(TimeControl.control_base('N=25000'), 25000)
        self.assertEqual(TimeControl.control_base('8.0+0.08'), 8.0)
        self.assertEqual(TimeControl.control_base('40/60.0+0.60'), 60.0)

class TimeBasedTests(SimpleTestCase):

    def workload(self, dev, base, upload_pgns='FALSE'):
        return SimpleNamespace(dev_time_control=dev, base_time_control=base, upload_pgns=upload_pgns)

    def test_nodes_and_depth_are_not_time_based(self):
        self.assertFalse(workload_uses_time_based_tc(self.workload('N=25000', 'D=12')))

    def test_any_clock_is_time_based(self):
        self.assertTrue(workload_uses_time_based_tc(self.workload('N=25000', '8.0+0.08')))
        self.assertTrue(workload_uses_time_based_tc(self.workload('MT=250', 'N=25000')))

    def test_verbose_pgns_measure_time(self):
        self.assertTrue(workload_uses_time_based_tc(self.workload('N=25000', 'N=25000', 'VERBOSE')))
