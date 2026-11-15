#!/usr/bin/env python3
"""Checks for the first-launch package prompt.

The package names are the ones in src/data/distro-packages.sh (also used by install.sh).
Install plans go through pkexec only. Nothing here installs a real package.
"""
import json, os, shutil, subprocess, sys, tempfile, textwrap, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'lib'))
import gvt_deps

GPU_SUMMARY = textwrap.dedent('''\
    GPU0:
    \tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    \tdeviceName         = AMD Radeon AI PRO R9700
    GPU1:
    \tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
    \tdeviceName         = llvmpipe (LLVM 18.1.8, 256 bits)
''')
LLVM_ONLY = textwrap.dedent('''\
    GPU0:
    \tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
    \tdeviceName         = llvmpipe (LLVM 18.1.8, 256 bits)
''')
FEDORA_GPU = [
    'git', 'cmake', 'gcc-c++', 'make', 'ninja-build', 'glslc',
    'vulkan-headers', 'vulkan-loader-devel', 'spirv-headers-devel',
    'vulkan-tools', 'mesa-vulkan-drivers',
]


class Result:
    def __init__(self, code=0, stdout='', stderr=''):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class FakeProbe:
    def __init__(self):
        self.os_release = 'ID=fedora\nID_LIKE=\nPRETTY_NAME="Fedora Linux 45"\n'
        self.cmds = {'pkexec': '/usr/bin/pkexec'}
        self.installed = set()
        self.vulkan = None
        self.nodes = []
        self.files = set()
        self.root = '/opt/gnome-voice-typing'
        self.help = {}
        self.apt_ver = '(none)'
        self.uinput = False
        self.pref = {}

    def install_root(self):
        return self.root

    def os_release_text(self):
        return self.os_release

    def which(self, name):
        return self.cmds.get(name)

    def render_nodes(self):
        return list(self.nodes)

    def exists(self, path):
        return path in self.files

    def access_write(self, path):
        return path == '/dev/uinput' and self.uinput

    def prefs(self):
        return dict(self.pref)

    def run(self, argv, timeout=20):
        if argv and argv[0] in ('rpm', 'dpkg', 'pacman'):
            return Result(0 if argv[-1] in self.installed else 1)
        if argv and (argv[0] == 'vulkaninfo' or argv[0].endswith('/vulkaninfo')):
            if self.vulkan is None:
                return Result(127, '', 'not found')
            return Result(0, self.vulkan, '')
        if argv[:2] == ['apt-cache', 'policy']:
            return Result(0, '  Candidate: %s\n' % self.apt_ver)
        if len(argv) > 1 and argv[1] == '--help':
            text = self.help.get(argv[0], '')
            return Result(0, text, '')
        return Result(1, '', 'unexpected %r' % (argv,))


class FakeRunner:
    def __init__(self, probe):
        self.probe = probe
        self.calls = []
        self.pkexec_code = 0
        self.pkexec_err = ''
        self.build_code = 0
        self.yd_code = 0
        self.model_there = False
        self.model_code = 0
        self.apply_code = 0

    def pkexec(self, argv):
        self.calls.append(('pkexec', list(argv)))
        if self.pkexec_code == 0:
            self.probe.installed.update(FEDORA_GPU)
            self.probe.installed.add('ydotool')
            self.probe.installed.update(['git', 'cmake', 'g++', 'make', 'ninja-build', 'libvulkan-dev', 'glslc', 'spirv-headers'])
            exe = '/usr/bin/ydotoold'
            self.probe.cmds['ydotoold'] = exe
            self.probe.help[exe] = 'ydotoold --socket-path PATH\n'
        return Result(self.pkexec_code, '', self.pkexec_err)

    def build(self, reporter):
        self.calls.append(('build',))
        reporter({'phase': 'building', 'message': 'Building the graphics-card engine…', 'detail': 'linking', 'ok': False, 'cancelled': False})
        if self.build_code == 0:
            self.probe.files.add(self.probe.root + '/gpu/libasrshim.so')
        return self.build_code

    def build_ydotool(self, reporter):
        self.calls.append(('ydotool',))
        if self.yd_code == 0:
            exe = self.probe.root + '/ext/bin/ydotoold'
            self.probe.files.add(exe)
            self.probe.help[exe] = '--socket-path'
        return self.yd_code

    def choose_model(self):
        self.calls.append(('choose',))
        return 'parakeet-gpu'

    def model_installed(self, mid):
        self.calls.append(('have-model', mid))
        return self.model_there

    def install_model(self, mid, reporter):
        self.calls.append(('install-model', mid))
        reporter({'phase': 'downloading', 'message': 'Downloading the graphics-card speech model…', 'detail': '40%', 'ok': False, 'cancelled': False})
        return self.model_code

    def apply_model(self, mid):
        self.calls.append(('apply', mid))
        return self.apply_code

    def log_tail(self):
        return 'build-gpu: missing glslc'


def names(calls, kind):
    return [c for c in calls if c[0] == kind]


class ListsTests(unittest.TestCase):
    def test_shared_lists_match_the_installer(self):
        lists = gvt_deps.load_lists(os.path.join(ROOT, 'src'))
        self.assertEqual(lists['dnf']['build'], [
            'git', 'cmake', 'gcc-c++', 'make', 'ninja-build', 'glslc',
            'vulkan-headers', 'vulkan-loader-devel', 'spirv-headers-devel'])
        self.assertEqual(lists['dnf']['gpu'], ['vulkan-tools', 'mesa-vulkan-drivers'])
        self.assertIn('ydotool', lists['dnf']['runtime'])
        self.assertEqual(lists['dnf']['query'], 'rpm -q --quiet')
        self.assertEqual(lists['apt']['build'], [
            'git', 'cmake', 'g++', 'make', 'ninja-build', 'libvulkan-dev', 'glslc', 'spirv-headers'])
        self.assertEqual(lists['apt']['query'], 'dpkg -s')
        self.assertEqual(lists['apt']['ydotool_source'], ['git', 'cmake', 'g++', 'make'])
        self.assertIn('shaderc', lists['pacman']['build'])
        self.assertEqual(lists['pacman']['query'], 'pacman -Qi')
        # install.sh no longer keeps a second copy of the names
        with open(os.path.join(ROOT, 'install.sh')) as f:
            sh = f.read()
        self.assertIn('distro-packages.sh', sh)
        self.assertIn('gvt_distro_packages', sh)
        self.assertNotIn('vulkan-loader-devel', sh)
        self.assertNotIn('spirv-headers-devel', sh)

    def test_vulkan_summary_skips_software_rendering(self):
        self.assertEqual(gvt_deps.parse_vulkan_summary(GPU_SUMMARY), 'AMD Radeon AI PRO R9700')
        self.assertEqual(gvt_deps.parse_vulkan_summary(LLVM_ONLY), '')
        self.assertEqual(gvt_deps.parse_vulkan_summary(''), '')


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.p = FakeProbe()
        self.p.cmds['vulkaninfo'] = '/usr/bin/vulkaninfo'
        self.p.vulkan = GPU_SUMMARY

    def test_lists_exactly_the_missing_gpu_and_typing_packages(self):
        st = gvt_deps.assess(self.p)
        self.assertTrue(st['offer_gpu'])
        self.assertEqual(st['gpu']['name'], 'AMD Radeon AI PRO R9700')
        self.assertEqual(st['missing_gpu'], FEDORA_GPU)
        self.assertEqual(st['missing_typing'], ['ydotool'])
        self.assertEqual(st['install_packages'][0], 'ydotool')
        self.assertEqual(st['pkexec_argv'][0], 'pkexec')
        self.assertNotIn('sudo', st['pkexec_argv'])
        self.assertEqual(st['pkexec_argv'][1:4], ['dnf', 'install', '-y'])
        for name in ('cmake', 'glslc', 'vulkan-headers', 'vulkan-loader-devel', 'spirv-headers-devel', 'ydotool'):
            self.assertIn(name, st['pkexec_argv'])
        view = gvt_deps.present(st)
        self.assertEqual(view['title'], 'Set up the graphics-card version?')
        self.assertEqual(view['install_label'], 'Install')
        self.assertEqual(view['not_now_label'], 'Not now')
        blob = '\n'.join(view['paragraphs'])
        self.assertIn('processor', blob)
        self.assertIn('cannot be built', blob)
        self.assertIn('remote-control', blob)
        listed = [n for g in view['groups'] for n in g['packages']]
        self.assertEqual(listed, st['missing_gpu'] + st['missing_typing'])

    def test_no_vulkan_gpu_does_not_offer_the_build(self):
        self.p.vulkan = LLVM_ONLY
        self.p.nodes = ['/dev/dri/renderD128']
        st = gvt_deps.assess(self.p)
        self.assertFalse(st['offer_gpu'])
        self.assertEqual(st['missing_gpu'], [])
        self.assertEqual(st['install_packages'], ['ydotool'])
        self.assertEqual(st['pkexec_argv'], ['pkexec', 'dnf', 'install', '-y', 'ydotool'])
        self.assertNotIn('cmake', st['pkexec_argv'])
        view = gvt_deps.present(st)
        self.assertEqual(view['title'], 'Install ydotool for typing?')
        self.assertNotIn('graphics-card build', json.dumps(view['groups']))

    def test_missing_vulkaninfo_with_no_device_is_not_a_gpu(self):
        self.p.cmds.pop('vulkaninfo')
        self.p.vulkan = None
        self.p.nodes = []
        st = gvt_deps.assess(self.p)
        self.assertEqual(st['gpu']['state'], 'absent')
        self.assertFalse(st['offer_gpu'])
        self.assertEqual(st['missing_gpu'], [])

    def test_render_node_without_vulkaninfo_still_offers_the_build(self):
        self.p.cmds.pop('vulkaninfo')
        self.p.nodes = ['/dev/dri/renderD128']
        st = gvt_deps.assess(self.p)
        self.assertEqual(st['gpu']['state'], 'unknown')
        self.assertTrue(st['offer_gpu'])
        self.assertIn('glslc', st['missing_gpu'])
        blob = '\n'.join(gvt_deps.present(st)['paragraphs'])
        self.assertIn('could not be checked', blob)

    def test_pkexec_missing_is_a_manual_command_and_not_sudo(self):
        self.p.cmds.pop('pkexec')
        st = gvt_deps.assess(self.p)
        self.assertFalse(st['pkexec'])
        self.assertIsNone(st['pkexec_argv'])
        self.assertTrue(st['manual_command'].startswith('sudo dnf install -y '))
        self.assertIn('ydotool', st['manual_command'])
        view = gvt_deps.present(st)
        self.assertEqual(view['install_label'], 'Check again')
        self.assertIn('pkexec', '\n'.join(view['paragraphs']))
        self.assertIn('sudo will not be run', '\n'.join(view['paragraphs']))

    def test_engine_already_built_skips_gpu_packages(self):
        self.p.files.add(self.p.root + '/gpu/libasrshim.so')
        exe = '/usr/bin/ydotoold'
        self.p.cmds['ydotoold'] = exe
        self.p.help[exe] = 'ok --socket-path\n'
        st = gvt_deps.assess(self.p)
        self.assertTrue(st['gpu_built'])
        self.assertTrue(st['typing_ok'])
        self.assertEqual(st['missing_gpu'], [])
        self.assertEqual(st['install_packages'], [])
        self.assertFalse(st['needs_attention'])
        self.assertFalse(st['should_auto_prompt'])

    def test_built_ydotool_counts_even_if_the_package_is_absent(self):
        exe = self.p.root + '/ext/bin/ydotoold'
        self.p.files.add(exe)
        self.p.help[exe] = 'usage: --socket-path\n'
        self.p.files.add(self.p.root + '/gpu/libasrshim.so')
        st = gvt_deps.assess(self.p)
        self.assertTrue(st['typing_ok'])
        self.assertNotIn('ydotool', st['install_packages'])

    def test_not_now_and_already_shown_do_not_auto_prompt(self):
        st = gvt_deps.assess(self.p, prefs={'dismissed': True})
        self.assertTrue(st['needs_attention'])
        self.assertFalse(st['should_auto_prompt'])
        view = gvt_deps.present(st)
        self.assertIn('System packages', view['footnote'])
        self.assertTrue(view['settings_line'])
        st = gvt_deps.assess(self.p, prefs={'prompted': True})
        self.assertFalse(st['should_auto_prompt'])

    def test_apt_old_ydotool_is_built_from_source_not_installed(self):
        self.p.os_release = 'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04"\n'
        self.p.cmds['apt-cache'] = '/usr/bin/apt-cache'
        self.p.apt_ver = '0.1.8-1'
        self.p.cmds.pop('vulkaninfo')
        self.p.nodes = []
        st = gvt_deps.assess(self.p)
        self.assertTrue(st['build_ydotool'])
        self.assertNotIn('ydotool', st['install_packages'])
        self.assertEqual(st['install_packages'], ['git', 'cmake', 'g++', 'make'])
        self.assertEqual(st['pkexec_argv'][0], 'pkexec')
        self.assertEqual(st['pkexec_argv'][1], 'bash')
        self.assertNotIn('sudo', st['pkexec_argv'])
        self.assertIn('apt-get install -y', st['pkexec_argv'][-1])
        self.assertNotIn('ydotool', st['pkexec_argv'][-1])
        self.assertEqual(gvt_deps.present(st)['title'], 'Build ydotool for typing?')

    def test_unknown_distro_does_not_invent_a_package_manager(self):
        self.p.os_release = 'ID=slackware\nPRETTY_NAME="Slackware"\n'
        self.p.cmds.pop('vulkaninfo')
        st = gvt_deps.assess(self.p)
        self.assertEqual(st['pm'], '')
        self.assertIsNone(st['pkexec_argv'])
        self.assertEqual(st['manual_command'], '')
        self.assertIn('not Fedora', '\n'.join(gvt_deps.present(st)['paragraphs']))

    def test_rejects_packages_outside_the_distro_list(self):
        spec = gvt_deps.load_lists(os.path.join(ROOT, 'src'))['dnf']
        with self.assertRaises(ValueError):
            gvt_deps.package_command('dnf', ['sudo'], spec)
        with self.assertRaises(ValueError):
            gvt_deps.package_command('dnf', ['cmake;rm'], spec)
        with self.assertRaises(ValueError):
            gvt_deps.package_command('dnf', ['not-a-real-package'], spec)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.p = FakeProbe()
        self.p.cmds['vulkaninfo'] = '/usr/bin/vulkaninfo'
        self.p.vulkan = GPU_SUMMARY
        self.r = FakeRunner(self.p)
        self.events = []

    def report(self, doc):
        self.events.append(dict(doc))

    def test_install_builds_and_switches(self):
        gvt_deps.run_job(self.p, self.r, self.report)
        self.assertEqual(self.r.calls[0][0], 'pkexec')
        self.assertEqual(self.r.calls[0][1][0], 'pkexec')
        self.assertNotIn('sudo', self.r.calls[0][1])
        self.assertIn(('build',), self.r.calls)
        self.assertIn(('install-model', 'parakeet-gpu'), self.r.calls)
        self.assertIn(('apply', 'parakeet-gpu'), self.r.calls)
        self.assertEqual(self.events[-1]['phase'], 'done')
        self.assertIn('graphics-card', self.events[-1]['message'])
        phases = [e['phase'] for e in self.events]
        self.assertIn('installing', phases)
        self.assertIn('building', phases)
        self.assertIn('downloading', phases)
        self.assertIn('switching', phases)

    def test_cancelled_password_does_not_build(self):
        self.r.pkexec_code = gvt_deps.PKEXEC_DISMISSED
        gvt_deps.run_job(self.p, self.r, self.report)
        self.assertEqual(names(self.r.calls, 'pkexec').__len__(), 1)
        self.assertFalse(names(self.r.calls, 'build'))
        self.assertEqual(self.events[-1]['phase'], 'error')
        self.assertTrue(self.events[-1]['cancelled'])
        self.assertIn('password window was closed', self.events[-1]['message'])

    def test_denied_password_does_not_build(self):
        self.r.pkexec_code = gvt_deps.PKEXEC_DENIED
        gvt_deps.run_job(self.p, self.r, self.report)
        self.assertFalse(names(self.r.calls, 'build'))
        self.assertTrue(self.events[-1]['cancelled'])
        self.assertIn('Permission was not granted', self.events[-1]['message'])

    def test_build_failure_is_reported_and_does_not_switch(self):
        self.r.build_code = 5
        gvt_deps.run_job(self.p, self.r, self.report)
        self.assertFalse(names(self.r.calls, 'apply'))
        self.assertIn('build failed', self.events[-1]['message'])
        self.assertIn('glslc', self.events[-1]['message'])

    def test_no_gpu_installs_ydotool_only(self):
        self.p.cmds.pop('vulkaninfo')
        self.p.vulkan = None
        self.p.nodes = []
        gvt_deps.run_job(self.p, self.r, self.report)
        argv = self.r.calls[0][1]
        self.assertEqual(argv, ['pkexec', 'dnf', 'install', '-y', 'ydotool'])
        self.assertFalse(names(self.r.calls, 'build'))
        self.assertEqual(self.events[-1]['phase'], 'done')

    def test_no_pkexec_never_calls_the_runner(self):
        self.p.cmds.pop('pkexec')
        gvt_deps.run_job(self.p, self.r, self.report)
        self.assertEqual(self.r.calls, [])
        self.assertEqual(self.events[-1]['phase'], 'error')
        self.assertIn('pkexec', self.events[-1]['message'])
        self.assertIn('sudo dnf install -y', self.events[-1]['message'])

    def test_gpu_absent_after_install_skips_the_build(self):
        self.p.cmds.pop('vulkaninfo')
        self.p.nodes = ['/dev/dri/renderD128']

        def pkexec(argv):
            self.r.calls.append(('pkexec', list(argv)))
            self.p.installed.update(FEDORA_GPU)
            self.p.installed.add('ydotool')
            self.p.cmds['vulkaninfo'] = '/usr/bin/vulkaninfo'
            self.p.vulkan = LLVM_ONLY
            exe = '/usr/bin/ydotoold'
            self.p.cmds['ydotoold'] = exe
            self.p.help[exe] = '--socket-path\n'
            return Result(0, '', '')

        self.r.pkexec = pkexec
        gvt_deps.run_job(self.p, self.r, self.report)
        self.assertTrue(names(self.r.calls, 'pkexec'))
        self.assertFalse(names(self.r.calls, 'build'))
        self.assertEqual(self.events[-1]['phase'], 'done')
        self.assertIn('No Vulkan graphics card', self.events[-1]['message'])

    def test_prefs_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(), 'system-deps.json')
        gvt_deps.update_prefs(path, dismissed=True, prompted=True)
        self.assertEqual(gvt_deps.load_prefs(path)['dismissed'], True)
        gvt_deps.update_prefs(path, prompted=True)
        self.assertTrue(gvt_deps.load_prefs(path)['dismissed'])


class InstallerTests(unittest.TestCase):
    def test_scripts_parse(self):
        for rel in ('install.sh', 'src/data/distro-packages.sh'):
            r = subprocess.run(['bash', '-n', os.path.join(ROOT, rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        for rel in ('src/lib/gvt_deps.py', 'src/bin/system-deps'):
            r = subprocess.run([sys.executable, '-m', 'py_compile', os.path.join(ROOT, rel)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_dry_run_prints_the_shared_list_and_does_not_run_sudo(self):
        with open('/etc/os-release') as f:
            osr = f.read().lower()
        if any(s in osr for s in ('fedora', 'rhel', 'centos')):
            pm, gpu_names, cpu_absent = 'dnf', ('cmake', 'glslc', 'vulkan-headers', 'vulkan-loader-devel', 'spirv-headers-devel', 'sudo dnf install -y'), ('glslc', 'vulkan-loader-devel')
        elif any(s in osr for s in ('arch', 'manjaro')):
            pm, gpu_names, cpu_absent = 'pacman', ('cmake', 'shaderc', 'vulkan-headers', 'spirv-headers', 'sudo pacman -S'), ('shaderc', 'spirv-headers')
        elif any(s in osr for s in ('debian', 'ubuntu')):
            pm, gpu_names, cpu_absent = 'apt', ('cmake', 'glslc', 'libvulkan-dev', 'spirv-headers', 'sudo apt-get install -y'), ('glslc', 'libvulkan-dev')
        else:
            self.skipTest('installer package prompt is only asserted on Fedora, Debian, Ubuntu and Arch')
        bindir = tempfile.mkdtemp()
        log = os.path.join(bindir, 'sudo.log')
        for name in ('dpkg', 'rpm', 'pacman', 'sudo'):
            path = os.path.join(bindir, name)
            with open(path, 'w') as f:
                if name == 'sudo':
                    f.write('#!/bin/sh\necho "SUDO-RAN $*" >> "$GVT_SUDO_LOG"\nexit 0\n')
                else:
                    f.write('#!/bin/sh\nexit 1\n')
            os.chmod(path, 0o755)
        env = dict(os.environ)
        env['PATH'] = bindir + os.pathsep + env.get('PATH', '')
        env['GVT_SUDO_LOG'] = log
        common = ['--dry-run', '--yes', '--skip-gnome', '--no-models', '--no-udev', '--no-start', '--no-autostart']
        gpu = subprocess.run(['bash', os.path.join(ROOT, 'install.sh'), '--gpu', *common],
                             cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(gpu.returncode, 0, gpu.stdout + gpu.stderr)
        for name in gpu_names:
            self.assertIn(name, gpu.stdout, pm)
        self.assertTrue('built from source' in gpu.stdout or 'ydotool' in gpu.stdout)
        self.assertFalse(os.path.exists(log), 'dry-run must not execute sudo')
        cpu = subprocess.run(['bash', os.path.join(ROOT, 'install.sh'), '--cpu-only', *common],
                             cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(cpu.returncode, 0, cpu.stdout + cpu.stderr)
        for name in cpu_absent:
            self.assertNotIn(name, cpu.stdout)
        self.assertFalse(os.path.exists(log))
        shutil.rmtree(bindir)

    def test_extension_wires_the_prompt(self):
        with open(os.path.join(ROOT, 'src/extension/extension.js')) as f:
            js = f.read()
        for needle in ('system-deps', 'should_auto_prompt', 'Not now', 'System packages', '_depsInstall', 'mark-prompted', 'deps-job.json'):
            self.assertIn(needle, js)


if __name__ == '__main__':
    unittest.main()
