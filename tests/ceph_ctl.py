import getpass
import logging
import shutil
import tempfile
import time
import psutil
from itertools import chain
import yaml
from subprocess import Popen, PIPE
from utils import wait_until_true, run_once
import json

from minion_sim.sim import MinionSim
from minion_sim.log import log as minion_sim_log
from django.utils.unittest.case import SkipTest
from tests.config import TestConfig

config = TestConfig()
logging.basicConfig()

log = logging.getLogger(__name__)

handler = logging.FileHandler("minion_sim.log")
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s %(message)s"))
minion_sim_log.addHandler(handler)


class CephControl(object):
    """
    Interface for tests to control one or more Ceph clusters under test.

    This can either be controlling the minion-sim, running unprivileged
    in a development environment, or it can be controlling a real life
    Ceph cluster.

    Some configuration arguments may be interpreted by a
    dev implementation as a "simulate this", while a real-cluster
    implementation might interpret them as "I require this state, skip
    the test if this cluster can't handle that".
    """

    def configure(self, server_count, cluster_count=1):
        """
        Tell me about the kind of system you would like.

        We will give you that system in a clean state or not at all:
        - Sometimes by setting it up for you here and now
        - Sometimes by cleaning up an existing cluster that's left from a previous test
        - Sometimes a clean cluster is already present for us
        - Sometimes we may not be able to give you the configuration you asked for
          (maybe you asked for more servers than we have servers) and have to
          throw you a test skip exception
        - Sometimes we may have a cluster that we can't clean up well enough
          to hand back to you, and have to throw you an error exception
        """
        raise NotImplementedError()

    def shutdown(self):
        """
        This cluster will not be used further by the test.

        If you created a cluster just for the test, tear it down here.  If the
        cluster was already up, just stop talking to it.
        """
        raise NotImplementedError()

    def mark_osd_in(self, fsid, osd_id, osd_in=True):
        raise NotImplementedError()

    def get_server_fqdns(self):
        raise NotImplementedError()

    def go_dark(self, fsid, dark=True, minion_id=None):
        """
        Create the condition where network connectivity between
        the calamari server and the ceph cluster is lost.
        """
        pass

    def get_fqdns(self, fsid):
        """
        Return all the FQDNs of machines with salt minion
        """
        raise NotImplementedError()


class EmbeddedCephControl(CephControl):
    """
    One or more simulated ceph clusters
    """

    def __init__(self):
        self._config_dirs = {}
        self._sims = {}

    def configure(self, server_count, cluster_count=1):
        osds_per_host = 4

        for i in range(0, cluster_count):
            domain = "cluster%d.com" % i
            config_dir = tempfile.mkdtemp()
            sim = MinionSim(config_dir, server_count, osds_per_host, port=8761 + i, domain=domain)
            fsid = sim.cluster.fsid
            self._config_dirs[fsid] = config_dir
            self._sims[fsid] = sim
            sim.start()

    def shutdown(self):
        log.info("%s.shutdown" % self.__class__.__name__)

        for sim in self._sims.values():
            sim.stop()
            sim.join()

        log.debug("lingering processes: %s" %
                  [p.name for p in psutil.process_iter() if p.username == getpass.getuser()])
        # Sleeps in tests suck... this one is here because the salt minion doesn't give us a nice way
        # to ensure that when we shut it down, subprocesses are complete before it returns, and even
        # so we can't be sure that messages from a dead minion aren't still winding their way
        # to cthulhu after this point.  So we fudge it.
        time.sleep(5)

        for config_dir in self._config_dirs.values():
            shutil.rmtree(config_dir)

    def get_server_fqdns(self):
        return list(chain(*[s.get_minion_fqdns() for s in self._sims.values()]))

    def mark_osd_in(self, fsid, osd_id, osd_in=True):
        self._sims[fsid].cluster.set_osd_state(osd_id, osd_in=1 if osd_in else 0)

    def go_dark(self, fsid, dark=True, minion_id=None):
        if minion_id:
            if dark:
                self._sims[fsid].halt_minion(minion_id)
            else:
                self._sims[fsid].start_minion(minion_id)
        else:
            if dark:
                self._sims[fsid].halt_minions()
            else:
                self._sims[fsid].start_minions()

        # Sleeps in tests suck... this one is here because the salt minion doesn't give us a nice way
        # to ensure that when we shut it down, subprocesses are complete before it returns, and even
        # so we can't be sure that messages from a dead minion aren't still winding their way
        # to cthulhu after this point.  So we fudge it.
        time.sleep(5)

    def get_fqdns(self, fsid):
        return self._sims[fsid].get_minion_fqdns()

    def get_service_fqdns(self, fsid, service_type):
        return self._sims[fsid].cluster.get_service_fqdns(service_type)


class ExternalCephControl(CephControl):
    """
    This is the code that talks to a cluster. It is currently dependent on teuthology
    """

    def __init__(self):
        with open(config.get('testing', 'external_cluster_path')) as f:
            self.config = yaml.load(f)

        # TODO parse this out of the cluster.yaml
        self.cluster_name = 'ceph'

    def _run_command(self, target, command):
        ssh_command = 'ssh ubuntu@{target} {command}'.format(target=target, command=command)
        proc = Popen(ssh_command, shell=True, stdout=PIPE)
        out, err = proc.communicate()
        if proc.returncode != 0:
            log.error("stdout: %s" % out)
            log.error("stderr: %s" % err)
            raise RuntimeError("Error {0} running {1}:'{2}'".format(
                proc.returncode, target, command
            ))
        else:
            log.info(err)

        return out

    def configure(self, server_count, cluster_count=1):

        # I hope you only wanted three, because I ain't buying
        # any more servers...
        if server_count != 3 or cluster_count != 1:
            raise SkipTest('ExternalCephControl does not multiple clusters or clusters with more than three nodes')

        self._bootstrap(self.config['master_fqdn'])
        self.restart_minions()

        self.reset_all_osds(self._run_command(self._get_admin_node(),
                                              "ceph --cluster {cluster} osd dump -f json-pretty".format(
                                                  cluster=self.cluster_name)))

        # Ensure all OSDs are initially up: assertion per #7813
        self._wait_for_state(lambda: self._run_command(self._get_admin_node(),
                                                       "ceph --cluster {cluster} osd dump -f json-pretty".format(
                                                           cluster=self.cluster_name)),
                             self._check_osds_in_and_up)
        # TODO what about tests that create OSDs we should remove them

        self.reset_all_pools(self._run_command(self._get_admin_node(),
                                               "ceph --cluster {cluster} osd lspools -f json-pretty".format(
                                                   cluster=self.cluster_name)))

        # Ensure there are initially no pools but the default ones. assertion per #7813
        self._wait_for_state(lambda: self._run_command(self._get_admin_node(),
                                                       "ceph --cluster {cluster} osd lspools -f json-pretty".format(
                                                           cluster=self.cluster_name)),
                             self._check_default_pools_only)

        # wait till all PGs are active and clean assertion per #7813
        # TODO stop scraping this, defer this because pg stat -f json-pretty is anything but
        self._wait_for_state(lambda: self._run_command(self._get_admin_node(),
                                                       "ceph --cluster {cluster} pg stat".format(
                                                           cluster=self.cluster_name)),
                             self._check_pgs_active_and_clean)

    def get_server_fqdns(self):
        return [target.split('@')[1] for target in self.config['cluster'].iterkeys()]

    def get_service_fqdns(self, fsid, service_type):
        # I run OSDs and mons in the same places (on all three servers)
        return self.get_server_fqdns()

    def shutdown(self):
        pass

    def get_fqdns(self, fsid):
        # TODO when we support multiple cluster change this
        return self.get_server_fqdns()

    def go_dark(self, fsid, dark=True, minion_id=None):
        action = 'stop' if dark else 'start'
        for target in self.get_fqdns(fsid):
            if minion_id and minion_id not in target:
                continue
            self._run_command(target, "sudo service salt-minion {action}".format(action=action))

    def _wait_for_state(self, command, state):
        log.info('Waiting for {state} on cluster'.format(state=state))
        wait_until_true(lambda: state(command()))

    def _check_default_pools_only(self, output):
        pools = json.loads(output)
        return {'data', 'metadata', 'rbd'} == set([x['poolname'] for x in pools])

    def _check_pgs_active_and_clean(self, output):
        _, total_stat, pg_stat, _ = output.replace(';', ':').split(':')
        return 'active+clean' == pg_stat.split()[1] and total_stat.split()[0] == pg_stat.split()[0]

    def _get_osds_down_or_out(self, output):
        osd_stat = json.loads(output)
        osd_down = [osd['osd'] for osd in osd_stat['osds'] if not osd['up']]
        osd_out = [osd['osd'] for osd in osd_stat['osds'] if not osd['in']]

        return {'down': osd_down, 'out': osd_out}

    def _check_osds_in_and_up(self, output):
        osd_state = self._get_osds_down_or_out(output)
        return not osd_state['down'] + osd_state['out']

    def reset_all_osds(self, output):
        target = self._get_admin_node()
        osd_stat = json.loads(output)
        # TODO this iteration should happen on the remote side
        for osd in osd_stat['osds']:
            self._run_command(target, 'ceph osd reweight {osd_id} 1.0'.format(osd_id=osd['osd']))
            self._run_command(target, 'ceph osd in {osd_id}'.format(osd_id=osd['osd']))

        for flag in ['pause']:
            self._run_command(target, "ceph --cluster ceph osd unset {flag}".format(flag=flag))

    def reset_all_pools(self, output):
        target = self._get_admin_node()
        default_pools = {'data', 'metadata', 'rbd'}
        pools = json.loads(output)
        existing_pools = self._get_pools(pools)

        for pool in default_pools - existing_pools:
            self._run_command(target, 'ceph osd pool create {pool} 64'.format(pool=pool))

        for pool in existing_pools - default_pools:
            self._run_command(target, 'ceph osd pool delete {pool} {pool} --yes-i-really-really-mean-it'.format(
                pool=pool))

    def restart_minions(self):
        for target in self.get_fqdns(None):
            self._run_command(target, 'sudo service salt-minion restart')

    @run_once
    def _bootstrap(self, master_fqdn):
        for target in self.get_fqdns(None):
            log.info('Bootstrapping salt-minion on {target}'.format(target=target))

            # TODO abstract out the port number
            output = self._run_command(target, '''"wget -O - http://{fqdn}:8000/bootstrap |\
             sudo python ;\
             sudo sed -i 's/^[#]*open_mode:.*$/open_mode: True/;s/^[#]*log_level:.*$/log_level: debug/' /etc/salt/minion && \
             sudo killall salt-minion; sudo service salt-minion restart"'''.format(fqdn=master_fqdn))
            log.info(output)

    def _get_admin_node(self):
        for target, roles in self.config['cluster'].iteritems():
            if 'client.0' in roles['roles']:
                return target.split('@')[1]

    def mark_osd_in(self, fsid, osd_id, osd_in=True):
        command = 'in' if osd_in else 'out'
        output = self._run_command(self._get_admin_node(),
                                   "ceph --cluster {cluster} osd {command} {id}".format(cluster=self.cluster_name,
                                                                                        command=command,
                                                                                        id=int(osd_id)))
        log.info(output)

    def _get_pools(self, output):
        return set([x['poolname'] for x in output])

if __name__ == "__main__":
    externalctl = ExternalCephControl()
    assert isinstance(externalctl.config, dict)
