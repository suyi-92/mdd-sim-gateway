"""Run the production Docker policy against command mocks, without a daemon/APT."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

MOCK = r'''#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['DOCKER_FIXTURE'])
s = json.loads((root/'state').read_text())
cmd, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root/'calls').open('a') as f:
    f.write(json.dumps([cmd, args, {k: os.environ.get(k) for k in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','HTTP_PROXY','HTTPS_PROXY')}])+'\n')
if cmd == 'dpkg-query':
    package = args[-1]
    if package in s.get('packages', []):
        print('install ok installed', end='')
    elif package == s.get('residual'):
        print('deinstall ok config-files', end='')
    elif package == s.get('partial'):
        print('install ok unpacked', end='')
    else:
        sys.exit(1)
elif cmd == 'systemctl':
    if args[0] == 'show':
        print({'LoadState': s.get('load','not-found'), 'ActiveState': s.get('active','inactive'), 'UnitFileState':s.get('unit','enabled')}[args[3]])
    elif args[0] == 'start':
        if s.get('start_fail'): sys.exit(1)
        s['active']='active'; s['healthy']=not s.get('never_healthy',False)
        (root/'state').write_text(json.dumps(s))
    elif args[0] != 'enable': sys.exit(91)
elif cmd == 'docker':
    assert args[:2] == ['--host','unix:///var/run/docker.sock'], args
    assert not any(os.environ.get(k) for k in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY'))
    if args[2:] == ['--version']:
        print('podman version 5' if s.get('podman') else 'Docker version 28.0.0')
    elif args[2] == 'info':
        assert '\n' in args[4] and r'\n' not in args[4], args[4]
        if not s.get('healthy'): sys.exit(1)
        print('linux', 'fixture-daemon', '/var/lib/docker', json.dumps(s.get('security',[])), s.get('os','Ubuntu'), sep='\n')
    else: sys.exit(92)
elif cmd == 'apt-get':
    if '--simulate' in args:
        if s.get('apt_fail'): sys.exit(1)
        print(s.get('plan', 'Inst docker.io (fixture)' if 'docker.io' in args else 'Inst curl (fixture)'))
    elif 'install' in args:
        assert '--no-remove' in args
        if 'docker.io' in args:
            s.update(packages=['docker.io','containerd','runc'],load='loaded')
        (root/'state').write_text(json.dumps(s))
    elif args[0] != 'update': sys.exit(93)
elif cmd == 'dockerd': pass
else: sys.exit(94)
'''


class DockerPolicyTests(unittest.TestCase):
    def run_policy(self, state, body='ensure_local_docker', library=None, extra=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'state').write_text(json.dumps(state))
            (root/'calls').touch()
            for name in ('docker','dockerd','systemctl','apt-get','dpkg-query'):
                path = root/name
                path.write_text(MOCK)
                path.chmod(0o755)
            lib = library or ROOT/'scripts/docker-local.sh'
            shell = r'''
set -Eeuo pipefail
source "$1"
# Substitute host filesystem probes; all executable decisions remain production code.
docker_socket_is_local() { return 0; }
docker_installation_evidence() { [[ "$TEST_EVIDENCE" == yes ]]; }
sleep() { SECONDS=$((SECONDS+40)); }
''' + extra + '\n' + body
            env = dict(os.environ, PATH=str(root)+':/usr/bin:/bin', DOCKER_FIXTURE=tmp,
                       TEST_EVIDENCE='yes' if state.get('evidence') else 'no',
                       DOCKER_HOST='tcp://remote.invalid:2376', DOCKER_CONTEXT='rootless',
                       DOCKER_TLS_VERIFY='1', HTTP_PROXY='http://proxy.invalid:7890',
                       HTTPS_PROXY='http://proxy.invalid:7890', VUB_DRY_RUN='false')
            result = subprocess.run(['bash','-c',shell,'bash',str(lib)],env=env,text=True,capture_output=True,timeout=15)
            calls = [json.loads(line) for line in (root/'calls').read_text().splitlines()]
            return result, calls

    def state(self, source='ce', active='active', **kw):
        packages = ['docker-ce','docker-ce-cli','containerd.io'] if source == 'ce' else ['docker.io','containerd','runc']
        return dict(packages=packages, load='loaded', active=active, healthy=active=='active', **kw)

    def no_mutations(self, calls):
        self.assertFalse([x for x in calls if x[0]=='apt-get' or (x[0]=='systemctl' and x[1][0]!='show')], calls)

    def test_fresh_safe_distro_install(self):
        result,calls=self.run_policy({})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('首次安装',result.stdout)
        apt=[x[1] for x in calls if x[0]=='apt-get']
        self.assertIn(['--simulate','--no-remove','install','docker.io'],apt)
        self.assertIn(['install','-y','--no-remove','docker.io'],apt)

    def test_healthy_sources_and_coexisting_rootless_keep_packages(self):
        for source in ('ce','distro'):
            for extras in ([],['docker-ce-rootless-extras']):
                with self.subTest(source=source,extras=extras):
                    state=self.state(source, residual='docker.io' if source=='ce' else 'docker-ce'); state['packages']+=extras
                    result,calls=self.run_policy(state)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('复用现有',result.stdout)
                    self.no_mutations(calls)
                    for call in calls:
                        if call[0]=='docker': self.assertEqual(call[2]['HTTP_PROXY'],'http://proxy.invalid:7890')

    def test_stopped_service_only_starts_existing(self):
        result,calls=self.run_policy(self.state(active='inactive'))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual([c[1] for c in calls if c[0]=='systemctl' and c[1][0]!='show'],[['start','docker.service']])
        self.assertFalse([c for c in calls if c[0]=='apt-get'])

    def test_broken_unknown_and_nonrootful_fail_without_migration(self):
        cases=[self.state(unit='masked'),self.state(active='failed'),self.state(load_override=True),
               self.state(partial='docker-ce-rootless-extras'),self.state(security=['name=rootless']),
               self.state(os='Docker Desktop'),self.state(podman=True),
               {'packages':['docker-ce-cli']},{'packages':['docker-ce-rootless-extras']},
               {'packages':['podman-docker']},{'evidence':True},
               {'packages':['docker.io'],'load':'not-found'},self.state()]
        cases[2]['load']='not-found'; cases[-1]['healthy']=False
        for state in cases:
            with self.subTest(state=state):
                result,calls=self.run_policy(state)
                self.assertNotEqual(result.returncode,0)
                self.no_mutations(calls)

    def test_start_failure_and_timeout_do_not_install(self):
        for kw in ({'start_fail':True},{'never_healthy':True}):
            result,calls=self.run_policy(self.state(active='inactive',**kw))
            self.assertNotEqual(result.returncode,0)
            self.assertFalse([c for c in calls if c[0]=='apt-get'])

    def test_apt_conflict_and_removal_never_execute_install(self):
        for state in ({'apt_fail':True},{'plan':'Remv containerd.io [fixture]'}):
            result,calls=self.run_policy(state)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse([c for c in calls if c[0]=='apt-get' and c[1][0]=='install'])

    def test_general_dependencies_cannot_change_docker(self):
        state=self.state(plan='Inst containerd.io [old] (new)')
        result,calls=self.run_policy(state,'docker_safe_apt_install preserve curl')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse([c for c in calls if c[0]=='apt-get' and c[1][0]=='install'])

    def test_no_cleanup_or_configuration_commands(self):
        # The command allowlist in MOCK fails if policy attempts any unrelated mutation.
        for state in ({},self.state(),self.state(active='inactive')):
            result,_=self.run_policy(state)
            self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless((ROOT/'scripts/00-lib.sh').exists(),'bootstrap-only source selection')
    def test_bootstrap_preserves_reverse_order_and_source_toggle(self):
        for source in ('ce','distro'):
            for upstream in ('true','false'):
                state=self.state(source)
                for _ in range(2):
                    result,calls=self.run_policy(state,
                        'select_docker_source; printf "chosen=%s\\n" "$DOCKER_SOURCE_KIND"; activate_docker_runtime',
                        ROOT/'scripts/00-lib.sh', f'ENABLE_UPSTREAM_APT_SOURCES={upstream}')
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('chosen='+source,result.stdout)
                    self.no_mutations(calls)

    @unittest.skipUnless((ROOT/'scripts/00-lib.sh').exists(),'bootstrap-only fresh selection')
    def test_bootstrap_fresh_source_rule(self):
        for upstream,expected in [('true','ce'),('false','distro')]:
            result,calls=self.run_policy({},'select_docker_source; echo "$DOCKER_SOURCE_KIND"',
                ROOT/'scripts/00-lib.sh',f'ENABLE_UPSTREAM_APT_SOURCES={upstream}')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),expected)
            self.no_mutations(calls)

    def test_actual_install_package_function_preserves_healthy_docker(self):
        source=(ROOT/'install.sh').read_text()
        function='install_packages() {'+source.split('install_packages() {',1)[1].split('\n}',1)[0]+'\n}'
        for family in ('ce','distro'):
            result,calls=self.run_policy(self.state(family),
                'ensure_local_docker; install_packages; docker_detect',
                extra='distro_key=fixture\ninfo() { printf "%s\n" "$*"; }\n'+function)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(result.stdout.rstrip().endswith('healthy-'+family),result.stdout)
            for cmd,args,_ in calls:
                if cmd=='apt-get':
                    self.assertFalse(set(args)&{'docker.io','docker-ce','containerd.io','containerd','runc'},args)
                if cmd=='systemctl' and args[0]!='show':
                    self.assertNotIn('docker.service',args)
                    self.assertNotIn('docker.socket',args)
