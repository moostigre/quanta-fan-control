"""Per-package temperature control reuses master's curves and PWM allocation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_controller import controller as fan, IDENTITY, ROOT


class PackageControlTests(unittest.TestCase):
    def setUp(self):
        fan.stop=False
        self.config=fan.validate_config(json.loads((ROOT/'config/quanta-fan-control.compilation.json').read_text()))
        self.config['cpu_control_mode']='curve'
        self.sensors={n:{'value':40,'unit':'degrees C','status':'ok'} for n in
            ['CPU0 Temp','CPU1 Temp','PCH Temp','Ambient_Temp','BP_Temp','Inlet_Temp','Exhaust_Temp']}
        self.sensors.update({n:{'value':4200,'unit':'RPM','status':'ok'} for n in self.config['required_fans']})
        self.control=fan.PackageFans()

    def tick(self,now,left=78,right=40,**kwargs):
        self.sensors['CPU0 Temp']['value']=left;self.sensors['CPU1 Temp']['value']=right
        return self.control.targets(self.config,{'0':[left]*13,'1':[right]*13},
            {n:s['value'] for n,s in self.sensors.items() if s['unit']=='degrees C'},now,**kwargs)

    def test_each_package_average_uses_only_its_own_readings(self):
        self.config['fan_staging']['enabled']=False
        targets=self.control.targets(self.config,{'0':[80,60],'1':[20,40]},
                                     {'CPU0 Temp':70,'CPU1 Temp':30,'PCH Temp':90},0)
        self.assertEqual(self.control.packages['0']['average_c'],70)
        self.assertEqual(self.control.packages['1']['average_c'],30)
        self.assertEqual(targets[2],30)
        self.assertEqual(targets[3],30)
        self.assertTrue(all(targets[i]==1 for i in [1,4,5,6]))

    def test_lead_speeds_follow_temperature_instead_of_an_rpm_target(self):
        for temp,duty in [(40,1),(60,20),(70,30),(78,42),(80,57)]:
            with self.subTest(temp=temp):
                self.control=fan.PackageFans()
                targets=self.tick(0,left=temp,right=40)
                self.assertEqual(targets[2],duty)
                self.assertEqual(targets[5],1)
                self.assertEqual(targets[1],1)
                self.assertEqual(targets[6],1)

    def test_helpers_have_independent_staging_and_outer_fans_never_join_normally(self):
        self.tick(0)
        targets=self.tick(5)
        self.assertGreater(targets[3],1)
        self.assertEqual(targets[4],1)
        for now in [10,30,60,120]:
            targets=self.tick(now,left=80)
            self.assertEqual(targets[1],1)
            self.assertEqual(targets[6],1)
        targets=self.tick(121,left=80,right=78)
        self.assertEqual(targets[4],1)
        targets=self.tick(126,left=80,right=78)
        self.assertGreater(targets[4],1)
        self.assertEqual(targets[1],1)
        self.assertEqual(targets[6],1)

    def test_urgent_raw_reading_raises_only_that_cpus_normal_fans(self):
        targets=self.tick(0,left=83,right=40)
        self.assertEqual(self.control.urgent_fans,{2,3})
        self.assertTrue(all(targets[i]>=100/3 for i in [2,3]))
        self.assertTrue(all(targets[i]==1 for i in [1,4,5,6]))

    def test_averaging_cannot_hide_emergency_and_all_six_assist(self):
        targets=self.tick(0,left=85,right=40,emergency=True)
        self.assertEqual(set(targets.values()),{100})
        self.assertTrue(self.control.status()['fan_stages_active'][2])

    def test_balanced_allocator_has_only_two_normal_stages(self):
        self.config['fan_staging']['allocation_mode']='balanced'
        self.tick(0,left=80)
        targets=self.tick(5,left=80)
        self.assertGreater(targets[2],targets[3])
        self.assertGreater(targets[3],20)
        self.assertTrue(all(targets[i]==1 for i in [1,4,5,6]))

    def test_predictive_load_feedback_is_independent_between_cpus(self):
        self.config['cpu_control_mode']='predictive'
        self.tick(0,left=40,right=40,loads={'0':0,'1':0})
        for now in range(1,21):
            targets=self.tick(now,left=40,right=40,loads={'0':100,'1':0})
        self.assertGreater(self.control.packages['0']['cpu_load_filtered_percent'],0)
        self.assertEqual(self.control.packages['1']['cpu_load_filtered_percent'],0)
        self.assertGreater(targets[2],targets[5])
        self.assertEqual(targets[1],1)
        self.assertEqual(targets[6],1)

    def test_helper_priming_preserves_only_its_own_banks_cooling(self):
        self.config['fan_staging']['allocation_mode']='balanced'
        self.config['fan_staging']['rise_floor_percent']=20
        output=fan.StagedFans()
        with patch.object(fan,'command'):
            first={1:1,2:40,3:1,4:1,5:40,6:1}
            output.apply(self.config,40,0,targets=first,banks=fan.PackageFans.BANKS)
            left,right=output.raw[2],output.raw[5]
            next_targets={1:1,2:30,3:25,4:1,5:1,6:1}
            output.apply(self.config,30,5,targets=next_targets,banks=fan.PackageFans.BANKS)
            self.assertEqual(output.raw[3],13)  # helper crosses the existing 20% floor
            self.assertEqual(output.raw[2],left)  # retain primary airflow during handover
            self.assertLess(output.raw[5],right)  # the other CPU can still cool down

    def run_cycles(self,readings,package_error=False,write_error=False,isolated_peak=None):
        frames=[];calls=[]
        initial=(40,100,[40]*26,{'0':[40]*13,'1':[40]*13})
        samples=[];sensors=[]
        for left,right in readings:
            packages={'0':[left]*13,'1':[right]*13}
            if isolated_peak is not None:packages['0'][0]=isolated_peak
            flat=packages['0']+packages['1']
            samples.append((max(flat),100,flat,packages))
            s=copy.deepcopy(self.sensors);s['CPU0 Temp']['value']=left;s['CPU1 Temp']['value']=right;sensors.append(s)
        if package_error:samples[0]=RuntimeError('package missing')
        def send(*args):
            calls.append(args)
            if args==('mc','info'):return IDENTITY
            if write_error and args[3]=='0x2':raise RuntimeError('PWM write failed')
            return ''
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'status.json'
            def end(*args):
                frames.append(json.loads(state.read_text()))
                if len(frames)==len(readings):fan.stop=True
                return 0
            with patch.object(fan,'read_config',return_value=self.config), \
                 patch.object(fan,'command',side_effect=send), \
                 patch.object(fan,'temperature_sample',side_effect=[initial,*samples]), \
                 patch.object(fan,'read_sensors',side_effect=[self.sensors,*sensors]), \
                 patch.object(fan,'STATE',str(state)),patch.object(fan.CPULoad,'sample',return_value=0), \
                 patch.object(fan,'automatic') as automatic,patch.object(fan,'notify'), \
                 patch.object(fan,'remaining_delay',side_effect=end),patch('signal.signal'), \
                 patch('sys.argv',['controller']),contextlib.redirect_stdout(io.StringIO()):
                try:fan.main()
                finally:automatic.assert_called_once()
        return frames,calls

    def test_daemon_controls_each_bank_and_returns_auxiliary_fans_to_idle_after_emergency(self):
        frames,calls=self.run_cycles([(85,40),(70,40)])
        self.assertEqual(set(frames[0]['fan_raw'].values()),{63})
        final=frames[1]
        self.assertEqual(final['fan_control_mode'],'per_package')
        self.assertEqual(final['cpu_packages']['0']['average_c'],70)
        self.assertEqual(final['cpu_packages']['1']['average_c'],40)
        self.assertEqual(final['cpu_control_c'],70)
        self.assertEqual(final['fan_raw']['1'],1)
        self.assertEqual(final['fan_raw']['6'],1)
        self.assertIn(('raw','0x30','0xf9','0x00','0x3f'),calls)

    def test_missing_package_sensor_requests_all_fans_full_speed(self):
        frames,_=self.run_cycles([(70,40)],package_error=True)
        self.assertTrue(frames[0]['faults'])
        self.assertEqual(set(frames[0]['fan_raw'].values()),{63})

    def test_one_hot_core_still_triggers_emergency_despite_a_cool_package_average(self):
        frames,_=self.run_cycles([(40,40)],isolated_peak=85)
        self.assertLess(frames[0]['cpu_packages']['0']['average_c'],50)
        self.assertEqual(set(frames[0]['fan_raw'].values()),{63})

    def test_partial_pwm_failure_restores_automatic_control(self):
        with self.assertRaisesRegex(RuntimeError,'PWM write failed'):
            self.run_cycles([(70,40)],write_error=True)

    def test_package_mode_is_default_and_explicit_global_fallback_is_validated(self):
        self.assertTrue(fan.package_control(self.config))
        self.config['cpu_package_control']=False
        self.assertFalse(fan.package_control(fan.validate_config(self.config)))
        self.config['cpu_package_control']='yes'
        with self.assertRaises(ValueError):fan.validate_config(self.config)


class PackageInputsTests(unittest.TestCase):
    def test_hwmon_identity_uses_package_label_and_requires_both_packages(self):
        with tempfile.TemporaryDirectory() as d:
            names=[]
            for index,package in enumerate([1,0]):
                p=Path(d)/str(index);p.mkdir()
                (p/'name').write_text('coretemp');names.append(str(p/'name'))
                (p/'temp1_label').write_text(f'Package id {package}')
                (p/'temp1_input').write_text(str((40+package*30)*1000))
                (p/'temp2_label').write_text('Core 0');(p/'temp2_input').write_text('40000')
                (p/'temp1_crit').write_text('94000')
            with patch.object(fan.glob,'glob',return_value=names):
                peak,critical,flat,packages=fan.core_temperatures(by_package=True)
                self.assertEqual((peak,critical),(70,94))
                self.assertEqual({k:sorted(v) for k,v in packages.items()},
                                 {'0':[40,40],'1':[40,70]})
                (Path(d)/'1/temp1_label').write_text('Package id 1')
                with self.assertRaises(RuntimeError):fan.core_temperatures(by_package=True)
                (Path(d)/'1/temp1_label').write_text('Package id 0')
                (Path(d)/'1/temp1_input').unlink()
                with self.assertRaises(RuntimeError):fan.core_temperatures(by_package=True)

    def test_cpu_load_is_sampled_per_physical_package(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);stat=p/'stat'
            for cpu in range(2):
                topo=p/f'cpu{cpu}/topology';topo.mkdir(parents=True)
                (topo/'physical_package_id').write_text(str(cpu))
            stat.write_text('cpu 0 0 0 200 0 0 0 0\ncpu0 0 0 0 100 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0\n')
            a=fan.CPULoad(str(stat),package=0,topology=p)
            b=fan.CPULoad(str(stat),package=1,topology=p)
            self.assertIsNone(a.sample());self.assertIsNone(b.sample())
            stat.write_text('cpu 100 0 0 300 0 0 0 0\ncpu0 100 0 0 100 0 0 0 0 90 0\ncpu1 0 0 0 200 0 0 0 0\n')
            self.assertEqual(a.sample(),100)
            self.assertEqual(b.sample(),0)
            (p/'cpu1/topology/physical_package_id').unlink()
            self.assertIsNone(b.sample())


if __name__=='__main__':unittest.main()
