import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fan', ROOT / 'quanta-fan-control.py')
fan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fan)
IDENTITY = 'Firmware Revision         : 7.54\nManufacturer ID           : 7244\nProduct ID                : 12882'


class PerCPUTests(unittest.TestCase):
    def setUp(self):
        fan.stop = False
        self.config = fan.validate_config(json.loads((ROOT/'config/quanta-fan-control.per-cpu.json').read_text()))
        self.control = fan.PerCPUFans()
        self.sensors = {name: {'value': 4200, 'unit': 'RPM', 'status': 'ok'} for name in self.config['required_fans']}
        self.sensors.update({name: {'value': 40, 'unit': 'degrees C', 'status': 'ok'} for name in
            ['CPU0 Temp', 'CPU1 Temp', 'PCH Temp', 'Ambient_Temp', 'BP_Temp', 'Inlet_Temp', 'Exhaust_Temp']})

    def temperatures(self, left=70, right=40):
        return {str(i): {'values': [t]*13, 'critical': 94} for i,t in enumerate([left,right])}

    def tick(self, now, left=70, right=40, **kwargs):
        self.sensors['CPU0 Temp']['value'] = left
        self.sensors['CPU1 Temp']['value'] = right
        return self.control.targets(self.config, self.temperatures(left,right), self.sensors, now, **kwargs)

    def rpm(self, selectors, value=6000, rotor=1):
        for i in selectors:self.sensors[f'SYS_FAN{i}-{rotor}']['value'] = value

    def test_hot_socket_only_engages_its_lead_module(self):
        out = self.tick(0)
        self.assertEqual(out[2],30)
        self.assertEqual({i:out[i] for i in [1,3,4,5,6]},dict.fromkeys([1,3,4,5,6],1))
        self.assertEqual(self.control.status()['fan_target_rpm'],{'2':6000})
        self.assertEqual(self.control.status()['cpu_sockets']['1']['active_fans'],[])

    def test_right_socket_uses_mirrored_5_4_6_order(self):
        self.tick(0,left=40,right=70)
        self.rpm([5]);self.tick(1,left=40,right=70);self.tick(11,left=40,right=70)
        self.assertEqual(self.control.banks['1']['count'],2)
        self.assertEqual(self.control.sockets['1']['active_fans'],[5,4])
        self.rpm([4]);self.tick(12,left=40,right=70);self.tick(22,left=40,right=70)
        self.assertEqual(self.control.sockets['1']['active_fans'],[5,4,6])
        self.assertEqual(self.control.sockets['0']['active_fans'],[])

    def test_helpers_wait_for_previous_stage_to_reach_measured_rpm(self):
        self.tick(0)
        self.tick(10)
        self.assertEqual(self.control.banks['0']['count'],1)
        self.rpm([2]);self.tick(11);self.tick(20)
        self.assertEqual(self.control.banks['0']['count'],1)
        self.tick(21)
        self.assertEqual(self.control.sockets['0']['active_fans'],[2,3])
        self.tick(31)
        self.assertEqual(self.control.banks['0']['count'],2)
        self.rpm([3]);self.tick(32);self.tick(42)
        self.assertEqual(self.control.sockets['0']['active_fans'],[2,3,1])
        self.assertEqual(self.control.banks['0']['rpm'],6000)
        self.rpm([1]);self.tick(43);self.tick(53)
        self.assertEqual(self.control.banks['0']['rpm'],7000)

    def test_stage_readiness_must_be_continuous(self):
        self.tick(0);self.rpm([2]);self.tick(1);self.tick(9)
        self.rpm([2],4000);self.tick(10)
        self.rpm([2]);self.tick(11);self.tick(20)
        self.assertEqual(self.control.banks['0']['count'],1)
        self.tick(21)
        self.assertEqual(self.control.banks['0']['count'],2)

    def test_unreachable_rpm_cannot_leave_hot_cpu_waiting_forever(self):
        self.tick(0);self.tick(29)
        self.assertEqual(self.control.banks['0']['count'],1)
        self.tick(30)
        self.assertEqual(self.control.banks['0']['count'],2)
        self.assertTrue(self.control.sockets['0']['assisted_after_timeout'])

    def test_cool_socket_has_its_own_reverse_order_release_timer(self):
        self.tick(0,left=83,right=70)
        self.assertEqual(self.control.banks['0']['count'],3)
        self.tick(1,left=40,right=70);self.tick(30,left=40,right=70)
        self.assertEqual(self.control.banks['0']['count'],3)
        self.tick(31,left=40,right=70)
        self.assertEqual(self.control.sockets['0']['active_fans'],[2,3])
        self.tick(32,left=40,right=70);self.tick(62,left=40,right=70)
        self.assertEqual(self.control.sockets['0']['active_fans'],[2])
        self.tick(63,left=40,right=70);self.tick(93,left=40,right=70)
        self.assertEqual(self.control.sockets['0']['active_fans'],[])
        self.assertGreater(self.control.banks['1']['count'],0)

    def test_urgent_socket_does_not_accelerate_the_cold_socket(self):
        out = self.tick(0,left=84,right=40)
        self.assertEqual(self.control.urgent_fans,{1,2,3})
        self.assertTrue(all(out[i]>=100*2/3 for i in [1,2,3]))
        self.assertTrue(all(out[i]==1 for i in [4,5,6]))

    def test_config_edit_resets_banks_and_feedback(self):
        self.tick(0,left=83)
        self.config['cpu_target_c']=66
        self.tick(1,left=70)
        self.assertEqual(self.control.sockets['0']['active_fans'],[2])
        self.assertEqual(set(self.control.feedback),{2})

    def test_rotor_choice_is_used_for_readiness_and_feedback(self):
        self.config['per_cpu_fans']['rpm_sensor']=2
        self.tick(0);self.rpm([2],6000,rotor=1);self.tick(1);self.tick(11)
        self.assertEqual(self.control.banks['0']['count'],1)
        self.rpm([2],6000,rotor=2);self.tick(12);self.tick(22)
        self.assertEqual(self.control.banks['0']['count'],2)
        self.assertEqual(self.control.status()['fan_rpm_sensor'],2)

    def test_manual_lead_override_does_not_block_assistance_or_wind_up(self):
        self.tick(0,raw={2:1},overrides={2:1})
        self.tick(1,raw={2:1},overrides={2:1});self.tick(11,raw={2:1},overrides={2:1})
        self.assertEqual(self.control.sockets['0']['active_fans'],[2,3])
        self.assertNotIn(2,self.control.feedback)

    def test_feedback_uses_tolerance_and_waits_for_the_output_to_settle(self):
        opt=fan.per_cpu_options(self.config)
        f=fan.RPMFeedback(opt,0)
        self.assertEqual(f.update(opt,6000,4000,5,1.6),30)
        self.assertEqual(f.update(opt,6000,4000,9,30),30)
        self.assertAlmostEqual(f.update(opt,6000,4000,10,30),30+100/63)
        self.assertAlmostEqual(f.update(opt,6000,5900,15,30+100/63),30+100/63)
        self.assertAlmostEqual(f.update(opt,6000,7000,20,30+100/63),30)

    def test_external_targets_preserve_independent_cooldown_and_manual_safety(self):
        output=fan.StagedFans()
        with patch.object(fan,'command'):
            output.apply(self.config,30,0,targets={1:30,2:30,3:1,4:1,5:1,6:1})
            before=output.raw[1]
            output.apply(self.config,30,3,targets={1:1,2:30,3:1,4:1,5:30,6:1})
            self.assertLess(output.raw[1],before)
            self.assertGreater(output.raw[5],1)
            result=output.apply(self.config,70,4,targets={i:70 if i<=3 else 1 for i in range(1,7)},
                                urgent_fans={1,2,3},overrides={2:1,5:10},force={2,5})
            self.assertGreater(result['2'],69)
            self.assertLess(result['5'],12)

    def test_rpm_feedback_recovers_through_pwm_hysteresis(self):
        # Run the real RPM loop and held PWM writer against a delayed tachometer.
        # Start with overspeed inside the writer's downward hysteresis window.
        output=fan.StagedFans()
        feedback=fan.RPMFeedback(fan.per_cpu_options(self.config),0)
        output.raw={i:1 for i in range(1,7)};output.raw[2]=20
        output.changed_at={i:0 for i in output.raw}
        rpm=6400
        samples=[]
        with patch.object(fan,'command'):
            for now in range(1,151):
                pct=feedback.update(fan.per_cpu_options(self.config),6000,rpm,now,output.raw[2]*100/63)
                targets={i:1 for i in range(1,7)};targets[2]=pct
                output.apply(self.config,pct,now,targets=targets)
                equilibrium=4200+200*max(0,output.raw[2]*100/63-21)
                rpm+=(equilibrium-rpm)/3
                samples.append(rpm)
        self.assertLess(min(samples[:40]),6200)
        self.assertTrue(all(5750<r<6250 for r in samples[-40:]))

    def test_average_temperature_is_local_to_each_socket(self):
        self.config['cpu_temperature_mode']='average'
        self.tick(0,left=70,right=40)
        self.assertEqual(self.control.sockets['0']['temperature_c'],70)
        self.assertEqual(self.control.sockets['1']['temperature_c'],40)
        self.assertEqual(self.control.sockets['0']['active_fans'],[2])

    def test_invalid_profiles_cannot_reach_hardware(self):
        for opt in [{'enabled':'yes'}, {'fan_order':[[2,3,1],[5,4,1]]},
                    {'fan_order':[[2,3,True],[5,4,6]]}, {'rpm_sensor':True},
                    {'lead_rpm':float('nan')}, {'max_rpm':5000}, {'rpm_control_interval_seconds':0},
                    {'stage_stall_seconds':15,'stage_up_hold_seconds':30}, {'unknown':1}]:
            with self.subTest(opt=opt):
                c=copy.deepcopy(self.config);c['per_cpu_fans'].update(opt)
                with self.assertRaises(ValueError):fan.validate_config(c)
        c=copy.deepcopy(self.config);c['fan_staging']['enabled']=True
        with self.assertRaises(ValueError):fan.validate_config(c)
        c=copy.deepcopy(self.config);c['required_fans'].remove('SYS_FAN2-2')
        with self.assertRaises(ValueError):fan.validate_config(c)

    def run_cycle(self, left=70,right=40,broken=None,package_failure=False,critical=100,write_failure=False):
        packages=self.temperatures(left,right)
        self.sensors['CPU0 Temp']['value']=left;self.sensors['CPU1 Temp']['value']=right
        broken_sensors=copy.deepcopy(self.sensors)
        if broken:broken_sensors[broken]['value']=None
        state=(max(left,right),critical,[left]*13+[right]*13,packages)
        initial_state=(40,critical,[40]*26,self.temperatures(40,40))
        initial_sensors=copy.deepcopy(self.sensors)
        initial_sensors['CPU0 Temp']['value']=initial_sensors['CPU1 Temp']['value']=40
        def end(*args):fan.stop=True;return 0
        def send(*args):
            if args==('mc','info'):return IDENTITY
            if write_failure and args[3]=='0x2':raise RuntimeError('module write failed')
            return ''
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'
            with patch.object(fan,'read_config',return_value=self.config), \
                 patch.object(fan,'command',side_effect=send) as command, \
                 patch.object(fan,'read_sensors',side_effect=[initial_sensors,broken_sensors]), \
                 patch.object(fan,'temperature_state',side_effect=[initial_state,RuntimeError('missing package') if package_failure else state]), \
                 patch.object(fan,'STATE',str(path)),patch.object(fan,'automatic') as automatic, \
                 patch.object(fan,'notify'),patch.object(fan,'remaining_delay',side_effect=end), \
                 patch('signal.signal'),patch('sys.argv',['controller']),contextlib.redirect_stdout(io.StringIO()):
                try:fan.main()
                finally:automatic.assert_called_once()
            return json.loads(path.read_text()),command.call_args_list

    def test_daemon_integrates_independent_rpm_control_and_status(self):
        status,_=self.run_cycle()
        self.assertEqual(status['fan_control_mode'],'per_cpu_rpm')
        self.assertEqual(status['fan_target_rpm'],{'2':6000})
        self.assertEqual(status['cpu_sockets']['0']['active_fans'],[2])
        self.assertEqual(status['cpu_sockets']['1']['active_fans'],[])
        self.assertGreater(status['fan_duty_percent']['2'],29)
        self.assertEqual(status['fan_duty_percent']['5'],1.6)

    def test_faults_and_one_socket_at_emergency_broadcast_full_speed(self):
        for opts in [{'left':85},{'left':70,'critical':80},
                     {'broken':'SYS_FAN5-2'},{'broken':'CPU1 Temp'}, {'package_failure':True}]:
            with self.subTest(opts=opts):
                fan.stop=False
                status,calls=self.run_cycle(**opts)
                self.assertEqual(set(status['fan_raw'].values()),{63})
                self.assertIn(('raw','0x30','0xf9','0x00','0x3f'),[c.args for c in calls])

    def test_partial_per_cpu_write_failure_restores_bmc_automatic_control(self):
        with self.assertRaisesRegex(RuntimeError,'module write failed'):
            self.run_cycle(write_failure=True)


class PackageSensorTests(unittest.TestCase):
    def test_package_labels_not_hwmon_order_identify_the_banks(self):
        with tempfile.TemporaryDirectory() as directory:
            names=[]
            for n,cpu in enumerate([1,0]):
                d=Path(directory)/str(n);d.mkdir()
                (d/'name').write_text('coretemp');names.append(str(d/'name'))
                (d/'temp1_label').write_text(f'Package id {cpu}')
                (d/'temp1_input').write_text(str((50+20*cpu)*1000))
                (d/'temp2_label').write_text('Core 0');(d/'temp2_input').write_text('50000')
                (d/'temp1_crit').write_text('94000')
            with patch.object(fan.glob,'glob',return_value=names):
                r=fan.package_temperatures()
                self.assertEqual(max(r['1']['values']),70)
                self.assertEqual(max(r['0']['values']),50)
                (Path(directory)/'1/temp1_label').write_text('Package id 1')
                with self.assertRaises(RuntimeError):fan.package_temperatures()
                (Path(directory)/'1/temp1_label').write_text('Package id 0')
                (Path(directory)/'1/temp1_input').unlink()
                with self.assertRaises(RuntimeError):fan.package_temperatures()


if __name__=='__main__':unittest.main()
