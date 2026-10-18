"""CPU averaging must suppress isolated spikes without hiding thermal faults."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_controller import controller as fan, IDENTITY, ROOT


class AverageTemperatureTests(unittest.TestCase):
    def setUp(self):
        fan.stop = False
        self.config = fan.validate_config(json.loads(
            (ROOT/'config/quanta-fan-control.compilation.json').read_text()))
        self.temperatures = {'CPU0 Temp':40,'CPU1 Temp':40,'PCH Temp':50,
                             'Ambient_Temp':20,'BP_Temp':25,'Inlet_Temp':30,'Exhaust_Temp':35}
        self.sensors = {name:dict(value=value,unit='degrees C',status='ok')
                        for name,value in self.temperatures.items()}
        self.sensors.update({name:dict(value=4200,unit='RPM',status='ok')
                             for name in self.config['required_fans']})

    def test_each_core_package_and_bmc_reading_has_equal_weight(self):
        values = [70,40,50,60,40,40]
        self.temperatures.update({'CPU0 Temp':65,'CPU1 Temp':55,'Ambient_Temp':1})
        maximum,average,control = fan.cpu_temperature_summary(self.config,values,self.temperatures)
        self.assertEqual(maximum,70)
        self.assertEqual(average,sum(values+[65,55])/8)
        self.assertEqual(control,average)
        self.config.pop('cpu_temperature_mode')
        self.assertEqual(fan.cpu_temperature_summary(self.config,values,self.temperatures)[2],70)

    def run_cycle(self, peak, staged=True, broken=False, critical=100):
        self.config['fan_staging']['enabled'] = staged
        values = [peak]+[40]*25  # individual cores plus package readings
        sensors = copy.deepcopy(self.sensors)
        sensors['CPU0 Temp']['value'] = peak
        cycle_sensors = copy.deepcopy(sensors)
        if broken:cycle_sensors['CPU1 Temp']['value'] = None
        def end_cycle(*args):
            fan.stop = True
            return 0
        with tempfile.TemporaryDirectory() as directory:
            status=Path(directory)/'status.json'
            with patch.object(fan,'read_config',return_value=self.config), \
                 patch.object(fan,'command',return_value=IDENTITY), \
                 patch.object(fan,'read_sensors',side_effect=[sensors,cycle_sensors]), \
                 patch.object(fan,'core_temperatures',side_effect=[(40,critical,[40]*26),(peak,critical,values)]), \
                 patch.object(fan.CPULoad,'sample',return_value=0), \
                 patch.object(fan,'STATE',str(status)), \
                 patch.object(fan,'automatic'),patch.object(fan,'notify'), \
                 patch.object(fan,'remaining_delay',side_effect=end_cycle), \
                 patch('signal.signal'),patch('sys.argv',['controller']), \
                 contextlib.redirect_stdout(io.StringIO()):
                fan.main()
            return json.loads(status.read_text())

    def test_isolated_70c_core_and_bmc_spike_does_not_raise_ordinary_fan_speed(self):
        s=self.run_cycle(70)
        self.assertEqual(s['cpu_temperature_mode'],'average')
        self.assertAlmostEqual(s['cpu_control_c'],(70*2+40*26)/28,places=2)
        self.assertEqual(s['cpu_control_c'],s['cpu_average_c'])
        self.assertEqual(s['cpu_max_c'],70)
        self.assertEqual(s['cpu_curve_demand_percent'],1)
        self.assertEqual(s['target_percent'],1)
        self.assertEqual(set(s['fan_raw'].values()),{1})

    def test_single_79c_core_still_forces_full_speed_in_staged_and_uniform_modes(self):
        for staged in (True,False):
            fan.stop=False
            s=self.run_cycle(79,staged=staged)
            self.assertLess(s['cpu_average_c'],60)
            self.assertEqual(s['target_percent'],100)
            self.assertEqual(set(s['fan_duty_percent'].values()),{100})

    def test_raw_near_emergency_guard_still_responds_to_single_hot_core(self):
        s=self.run_cycle(77)
        self.assertLess(s['cpu_average_c'],60)
        self.assertGreaterEqual(s['target_percent'],100/3-.1)
        self.assertEqual(s['cpu_controller_state'],'high_temperature')

    def test_missing_bmc_sensor_still_forces_full_speed(self):
        s=self.run_cycle(40,broken=True)
        self.assertTrue(s['faults'])
        self.assertEqual(set(s['fan_duty_percent'].values()),{100})

    def test_hardware_critical_margin_still_uses_peak_not_mean(self):
        s=self.run_cycle(70,critical=80)
        self.assertIn('CPU near hardware limit',s['faults'])
        self.assertEqual(s['target_percent'],100)

    def test_average_mode_is_validated(self):
        self.config['cpu_temperature_mode']='median'
        with self.assertRaises(ValueError):fan.validate_config(self.config)

    def test_hwmon_preserves_each_reading_and_rejects_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            names=[]
            for cpu in range(2):
                device=Path(directory)/str(cpu);device.mkdir()
                name=device/'name';name.write_text('coretemp');names.append(str(name))
                (device/'temp1_input').write_text(str((60+cpu)*1000))
                (device/'temp2_input').write_text(str((50+cpu)*1000))
                (device/'temp1_crit').write_text('94000')
            with patch.object(fan.glob,'glob',return_value=names):
                maximum,critical,values=fan.core_temperatures()
                self.assertEqual((maximum,critical),(61,94))
                self.assertEqual(sorted(values),[50,51,60,61])
                (Path(directory)/'0/temp2_input').write_text('nan')
                with self.assertRaises(RuntimeError):fan.core_temperatures()
