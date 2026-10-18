"""Airflow and timing regressions for the measured 6k/9k/12k profile."""
import json
import unittest
from unittest.mock import patch
from test_predictive import ROOT, fan


class CompilationTests(unittest.TestCase):
    def setUp(self):
        self.config = fan.validate_config(json.loads(
            (ROOT/'config/quanta-fan-control.compilation.json').read_text()))

    def test_measured_airflow_points_are_reached_before_the_heat_guard(self):
        for temperature, raw in ((55,19),(60,27),(65,36)):
            control, output = fan.AdaptiveCPU(temperature,0), fan.StagedFans()
            duty=1
            with patch.object(fan,'command'):
                output.apply(self.config,duty,0)
                for now in range(1,16):
                    duty=control.update(self.config,temperature,duty,now,0)
                    output.apply(self.config,duty,now)
                    self.assertNotEqual(control.state,'high_temperature')
            self.assertGreaterEqual(output.raw[3],raw)
            self.assertEqual(output.raw[3],output.raw[4])
            self.assertLess(max(output.raw.values()),63)

    def test_cold_start_crosses_dead_zone_immediately_then_keeps_small_rpm_steps(self):
        control,output=fan.AdaptiveCPU(55,0),fan.StagedFans()
        with patch.object(fan,'command'):
            output.apply(self.config,1,0)
            duty=control.update(self.config,55,1,1,0)
            self.assertEqual(duty,30)
            output.apply(self.config,duty,1)
            self.assertEqual(output.raw[3],19)
            output.apply(self.config,42,2)
            self.assertEqual(output.raw[3],22)
            output.apply(self.config,42,2.5)
            self.assertEqual(output.raw[3],22)

    def test_secondary_airflow_does_not_reduce_primary_rpm_command(self):
        output=fan.StagedFans()
        with patch.object(fan,'command'):
            for now in range(21): output.apply(self.config,57,now)
        self.assertEqual(output.raw[3],36)
        self.assertEqual(output.raw[4],36)
        self.assertGreaterEqual(output.raw[2],24)
        self.assertGreaterEqual(output.raw[5],24)
        self.assertLess(output.raw[1],output.raw[2])

    def test_step_down_and_fault_override_still_work(self):
        control,output=fan.AdaptiveCPU(78,0),fan.StagedFans()
        with patch.object(fan,'command'):
            duty=control.update(self.config,78,1,1,100)
            output.apply(self.config,duty,1,emergency=True)
            for now in range(2,62):
                duty=control.update(self.config,45,duty,now,0)
                output.apply(self.config,duty,now)
            self.assertLess(duty,5)
            self.assertLessEqual(max(output.raw.values()),3)
            output.apply(self.config,100,62,emergency=True,overrides={3:1})
            self.assertEqual(set(output.raw.values()),{63})

    def test_manual_settings_bypass_the_dead_zone_jump(self):
        output=fan.StagedFans()
        with patch.object(fan,'command'):
            output.apply(self.config,1,0)
            for now in range(1,16):output.apply(self.config,57,now,overrides={3:1})
        self.assertEqual(output.raw[3],1)
        self.assertEqual(output.raw[4],36)

    def test_floor_cannot_exceed_current_thermal_demand(self):
        control,output=fan.AdaptiveCPU(50,0),fan.StagedFans()
        duty=1
        with patch.object(fan,'command'):
            output.apply(self.config,duty,0)
            for now in range(1,16):
                duty=control.update(self.config,50,duty,now,0)
                output.apply(self.config,duty,now)
        self.assertEqual(duty,20)
        self.assertEqual(output.raw[3],13)
        self.config['fan_staging']['rise_floor_percent']=41
        with self.assertRaises(ValueError):fan.validate_config(self.config)
