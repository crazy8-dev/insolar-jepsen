#!/usr/bin/env python3
# vim: set ai et ts=4 sw=4:

import os
import sys
import subprocess
import argparse
import json
import time
import random
import traceback
import datetime

# Roles:
# jepsen-1: heavy
# jepsen-2: light
# jepsen-3: light
# jepsen-4: light
# jepsen-5: light
# jepsen-6: light
# jepsen-7: virtual (not-discovery)
# jepsen-8: virtual (not-discovery)
# jepsen-9: virtual (not-discovery)
# jepsen-10: virtual (not-discovery)
# jepsen-11: virtual (not-discovery)
# jepsen-12: pulsar

START_PORT = 32000
VIRTUAL_START_PORT = 19000
INSPATH = "go/src/github.com/insolar/insolar"
OLD_MEMBERS_FILE = ".artifacts/bench-members/members-from-start.txt"
MEMBERS_FILE = ".artifacts/bench-members/members.txt"
LIGHT_CHAIN_LIMIT = 5
PULSE_DELTA = 10

HEAVY = 1
LIGHTS = [2, 3, 4, 5, 6]
VIRTUALS = [7, 8, 9, 10, 11]

DISCOVERY_NODES = [HEAVY] + LIGHTS
NOT_DISCOVERY_NODES = VIRTUALS
NODES = DISCOVERY_NODES + NOT_DISCOVERY_NODES

PULSAR = 12
ALL_PODS = NODES + [PULSAR]

MIN_ROLES_VIRTUAL = 2
LOG_LEVEL = "Debug"  # Info
NAMESPACE = "default"
SLOW_NETWORK_SPEED = '4mbps'
FAST_NETWORK_SPEED = '1000mbps'
SMALL_MTU = 1400
NORMAL_MTU = 1500
DEBUG = False
POD_NODES = dict()  # is filled below
DEPENDENCIES = ['docker', 'kubectl', 'jq']
C = 5
R = 1

CURRENT_TEST_NAME = ""

K8S_YAML_TEMPLATE = """
kind: Service
apiVersion: v1
metadata:
  name: {pod_name}
spec:
  type: NodePort
  ports:
    - port: 22
      nodePort: {ssh_port}
  selector:
    name: {pod_name}
---
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  labels:
    name: {pod_name}
    app: insolar-jepsen
spec:
  containers:
    - name: {pod_name}
      image: {image_name}
      imagePullPolicy: {pull_policy}
      securityContext:
        capabilities:
          add:
            - NET_ADMIN
      ports:
        - containerPort: 22
  nodeSelector:
    jepsen: "true"
---
"""

# to make `sed` work properly, otherwise it failes with an error:
# sed: RE error: illegal byte sequence
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
os.environ["LC_CTYPE"] = "C"


def logto(fname, index=""):
    # `tee` is used to see recent logs in tmux. please keep it!
    return "2>&1 | tee /dev/tty | gzip --stdout > " + fname + "_`date +%s`.log.gz"


def start_test(msg):
    global CURRENT_TEST_NAME
    CURRENT_TEST_NAME = msg
    print("##teamcity[testStarted name='%s']" % CURRENT_TEST_NAME)


def fail_test(failure_message):
    global CURRENT_TEST_NAME
    notify("Test failed")
    msg = failure_message \
        .replace("|", "||").replace("'", "|'") \
        .replace("\n", "|n").replace("\r", "|r") \
        .replace("[", "|[").replace("]", "|]")
    print("##teamcity[testFailed name='%s' message='%s']" %
          (CURRENT_TEST_NAME, msg))
    trace = "".join(traceback.format_stack()[:-1]) \
        .replace("|", "||").replace("'", "|'") \
        .replace("\n", "|n").replace("\r", "|r") \
        .replace("[", "|[").replace("]", "|]")
    print("##teamcity[testFailed name='%s' message='%s']" %
          (CURRENT_TEST_NAME, trace))
    stop_test()
    info("Stops nodes after fail")
    for node in NODES:
        kill(node, "insolard")
    kill(PULSAR, "pulsard")
    wait_until_insolar_is_down()
    sys.exit(1)


def stop_test():
    global CURRENT_TEST_NAME
    print("##teamcity[testFinished name='%s']" % CURRENT_TEST_NAME)


def info(msg):
    print(str(datetime.datetime.now())+" INFO: "+str(msg))


def wait(nsec):
    info("waiting "+str(nsec)+" second"+("s" if nsec > 1 else "")+"...")
    time.sleep(nsec)


def notify(message):
    run("""(which osascript 2>/dev/null 1>&2) && osascript -e 'display notification " """ +
        message + """ " with title "Jepsen"' || true""")


def check(condition, failure_message):
    if not condition:
        fail_test(failure_message)


def check_alive(condition):
    if not condition:
        out = ssh_output(1, 'cd go/src/github.com/insolar/insolar && ' +
                         'timelimit -s9 -t10 ' +  # timeout: 10 seconds
                         './bin/pulsewatcher --single --config ./pulsewatcher.yaml')
        msg = "Insolar must be alive, but its not:\n" + out
        fail_test(msg)


def check_down(condition):
    if not condition:
        fail_test("Insolar must be down, but its not")


def check_benchmark(condition, out):
    if not condition:
        fail_test("Benchmark return error: \n" + out)


def debug(msg):
    if not DEBUG:
        return
    print("    "+msg)


def run(cmd):
    debug(cmd)
    proc = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        print("Command `%s` returned non-zero status: %d, output: %s" %
              (cmd, proc.returncode, str(proc.stdout)))
        info("Stops nodes after fail")
        for node in NODES:
            kill(node, "insolard")
        kill(PULSAR, "pulsard")
        wait_until_insolar_is_down()
        sys.exit(1)


def get_output(cmd):
    debug(cmd)
    proc = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print("Command `%s` returned non-zero status: %d, output: %s, error: %s" %
              (cmd, proc.returncode, str(proc.stdout), str(proc.stderr)))
    out = proc.stdout
    data = out.decode('utf-8').strip()
    return data


def ssh_user_host(pod):
    return "gopher@"+POD_NODES['jepsen-'+str(pod)]


def ssh(pod, cmd):
    run("ssh -tt -o 'StrictHostKeyChecking no' -i ./base-image/id_rsa -p" +
        str(START_PORT + pod)+" "+ssh_user_host(pod) +
        """ "bash -c 'source ./.bash_profile ; """ +
        cmd + """ '" """)


def ssh_output(pod, cmd):
    return get_output("ssh -tt -o 'StrictHostKeyChecking no' -i ./base-image/id_rsa -p" +
                      str(START_PORT + pod)+" "+ssh_user_host(pod) +
                      """ "bash -c 'source ./.bash_profile ; """ +
                      cmd + """ '" """)


def scp_to(pod, lpath, rpath, flags=''):
    run("scp -o 'StrictHostKeyChecking no' -i ./base-image/id_rsa -P" +
        str(START_PORT + pod)+" "+flags+" " + lpath + " "+ssh_user_host(pod) +
        ":"+rpath)


def scp_from(pod, rpath, lpath, flags=''):
    run("scp -o 'StrictHostKeyChecking no' -i ./base-image/id_rsa -P" +
        str(START_PORT + pod)+" " + flags + " "+ssh_user_host(pod) +
        ":"+rpath+" "+lpath)


def k8s():
    return "kubectl --namespace "+NAMESPACE+" "


def k8s_gen_yaml(fname, image_name, pull_policy):
    with open(fname, "w") as f:
        for i in ALL_PODS:
            pod_name = "jepsen-" + str(i)
            ssh_port = str(32000 + i)
            descr = K8S_YAML_TEMPLATE.format(
                pod_name=pod_name,
                ssh_port=ssh_port,
                image_name=image_name,
                pull_policy=pull_policy
            )
            f.write(descr)


def k8s_get_pod_ips():
    """
    Returns a map PodName -> PodIP
    """
    data = get_output(k8s()+"get pods -l app=insolar-jepsen -o=json | " +
                      """jq -r '.items[] | .metadata.name + " " + .status.podIP'""")
    res = {}
    for kv in data.split("\n"):
        [k, v] = kv.split(' ')
        res[k] = v
    return res


def k8s_get_pod_nodes():
    """
    Returns a map PodName -> NodeName
    """
    data = get_output(k8s() + "get pods -l app=insolar-jepsen -o=json | " +
                      """jq -r '.items[] | .metadata.name + " " + .spec.nodeName'""")
    res = {}
    for kv in data.split("\n"):
        [k, v] = kv.split(' ')
        if v == "docker-for-desktop":  # Docker Desktop 2.0, k8s 1.10, docker 18.09
            v = "localhost"
        if v == "docker-desktop":  # Docker Desktop 2.1, k8s 1.14, docker 19.03
            v = "localhost"
        res[k] = v
    return res


def k8s_stop_pods_if_running(fname):
    info("stopping pods if they are running")
    run(k8s()+"delete -f "+fname+" 2>/dev/null || true")
    for n in range(60):
        data = get_output(k8s()+"get pods -l app=insolar-jepsen -o=json | " +
                          "jq -r '.items[].metadata.name' | wc -l")
        info("running pods: "+data)
        if data == "0":
            break
        wait(1)
    else:
        fail_test("k8s_stop_pods_if_running no attempts left")
    wait(10)  # make sure services and everything else are gone as well


def k8s_start_pods(fname):
    info("starting pods")
    run(k8s()+"apply -f "+fname)
    for n in range(60):
        data = get_output(k8s()+"get pods -l app=insolar-jepsen -o=json | " +
                          "jq -r '.items[].status.phase' | grep Running | wc -l")
        info("running pods: "+data)
        if data == str(len(ALL_PODS)):
            break
        wait(1)
    else:
        fail_test("k8s_start_pods no attempts left")


def set_network_speed(pod, speed):
    ssh(pod, 'sudo tc qdisc del dev eth0 root || true')
    ssh(pod, 'sudo tc qdisc add dev eth0 root handle 1: tbf rate ' +
        speed+' latency 1ms burst 1540')
    ssh(pod, 'sudo tc qdisc del dev eth0 ingress || true')
    ssh(pod, 'sudo tc qdisc add dev eth0 ingress')
    ssh(pod, 'sudo tc filter add dev eth0 root protocol ip u32 match u32 0 0 police rate ' +
        speed+' burst 10k drop flowid :1')
    ssh(pod, 'sudo tc filter add dev eth0 parent ffff: protocol ip u32 match u32 0 0 police rate ' +
        speed+' burst 10k drop flowid :1')


def set_mtu(pod, mtu):
    ssh(pod, 'sudo ifconfig eth0 mtu '+str(mtu))


def create_simple_netsplit(pod, pod_ips):
    """
    Simulates simplest netsplit: one node is cut-off from the rest of the network
    """
    for current_pod in ALL_PODS:
        if current_pod == pod:
            continue
        current_ip = pod_ips['jepsen-'+str(current_pod)]
        ssh(pod, 'sudo iptables -A INPUT -s '+current_ip+' -j DROP && ' +
            'sudo iptables -A OUTPUT -d '+current_ip+' -j DROP')


def fix_simple_netsplit(pod, pod_ips):
    """
    Rolls back an effect of create_simple_netsplit()
    """
    for current_pod in ALL_PODS:
        if current_pod == pod:
            continue
        current_ip = pod_ips['jepsen-'+str(current_pod)]
        ssh(pod, 'sudo iptables -D INPUT -s '+current_ip+' -j DROP && ' +
            'sudo iptables -D OUTPUT -d '+current_ip+' -j DROP')


def old_node_is_down(status):
    if 'PulseNumber' in status and \
            'Error' in status:
        return status['PulseNumber'] == -1 and \
            status['Error'] != ''
    else:
        return 0


def new_node_is_down(status):
    if 'pulseNumber' in status:
        return status['pulseNumber'] == -1
    else:
        return 0


def node_is_down(status):
    return old_node_is_down(status) or new_node_is_down(status)


def old_node_status_is_ok(status, nodes_online):
    if 'NetworkState' in status and \
        'ActiveListSize' in status and \
        'WorkingListSize' in status and \
            'Error' in status:
        return status['NetworkState'] == 'CompleteNetworkState' and \
            status['ActiveListSize'] == nodes_online and \
            status['WorkingListSize'] == nodes_online and \
            status['Error'] == ''
    else:
        return 0


def new_node_status_is_ok(status, nodes_online):
    if 'networkState' in status and \
        'activeListSize' in status and \
            'workingListSize' in status:
        return status['networkState'] == 'CompleteNetworkState' and \
            status['activeListSize'] == nodes_online and \
            status['workingListSize'] == nodes_online
    else:
        return 0


def node_status_is_ok(status, nodes_online):
    return old_node_status_is_ok(status, nodes_online) or new_node_status_is_ok(status, nodes_online)


def network_status_is_ok(network_status, nodes_online):
    online_list = [network_status[nodeIndex-1]
                   for nodeIndex in nodes_online if not node_is_down(network_status[nodeIndex-1])]
    # make sure an expected number of nodes is online
    if len(online_list) < len(nodes_online):
        info("[NetworkStatus] error - {} nodes online, {} expected".format(len(online_list), nodes_online))
        return False

    # make sure all PulseNumber's are equal
    pn = set(s['PulseNumber'] for s in online_list)
    if len(pn) != 1:
        info("[NetworkStatus] PulseNumber's differ: " +
             str([s['PulseNumber'] for s in online_list]))
        return False
    else:
        info("[NetworkStatus] PulseNumber is " + str(pn))

    # check node statuses
    for nodeIndex in nodes_online:
        node_status = network_status[nodeIndex-1]
        if node_is_down(node_status):
            continue
        if not node_status_is_ok(node_status, len(nodes_online)):
            info("[NetworkStatus] Node status is not OK: "+str(node_status) +
                 "   (nodes online: "+str(nodes_online)+")")
            return False

    info("[NetworkStatus] Everything is OK")
    return True


def get_finalized_pulse_from_exporter():
    cmd = 'grpcurl -import-path /home/gopher/go/src' +\
          ' -proto /home/gopher/go/src/github.com/insolar/insolar/ledger/heavy/exporter/pulse_exporter.proto' +\
          """ -plaintext localhost:5678 exporter.PulseExporter.TopSyncPulse"""
    out = ssh_output(HEAVY, cmd)
    pulse = json.loads(out)["PulseNumber"]
    info("exporter said: " + str(pulse))
    return pulse


def run_benchmark(pod_ips, api_pod=VIRTUALS[0], ssh_pod=1, extra_args="", c=C, r=R, timeout=30, background=False):
    virtual_pod_name = 'jepsen-'+str(api_pod)
    port = VIRTUAL_START_PORT + api_pod
    out = ""
    try:
        out = ssh_output(ssh_pod, 'cd go/src/github.com/insolar/insolar && ' +
                         ("tmux new-session -d \"" if background else "") +
                         'timelimit -s9 -t'+str(timeout)+' ' +
                         './bin/benchmark -c ' + str(c) + ' -r ' + str(r) + ' -a http://'+pod_ips[virtual_pod_name] +
                         ':'+str(port) + '/admin-api/rpc ' +
                         ' -p http://'+pod_ips[virtual_pod_name]+':'+str(port + 100)+'/api/rpc ' +
                         '-k=./scripts/insolard/configs/ ' + extra_args +
                         ("\"" if background else ""))
    except Exception as e:
        print(e)
        out = "ssh_output() throwed an exception (non-zero return code): "+str(e)

    if background:
        return True, out

    if 'Successes: '+str(c*r) in out:
        return True, out
    return False, out


def pulsewatcher_output(ssh_pod=1):
    return ssh_output(ssh_pod, 'cd go/src/github.com/insolar/insolar && ' +
                      'timelimit -s9 -t10 ' +  # timeout: 10 seconds
                      './bin/pulsewatcher --single --json --config ./pulsewatcher.yaml')


def current_pulse(node_index=HEAVY, ssh_pod=1):
    network_status = json.loads(pulsewatcher_output(ssh_pod))
    pn = network_status[node_index]['PulseNumber']
    return pn


def insolar_is_alive(pod_ips, virtual_pod, nodes_online, ssh_pod=1):
    out = pulsewatcher_output(ssh_pod)
    network_status = json.loads(out)
    if not network_status_is_ok(network_status, nodes_online):
        info('insolar_is_alive() is false, out = "'+out+'"')
        return False

    ok, out = run_benchmark(pod_ips, virtual_pod, ssh_pod)
    if ok:
        return True
    else:
        info("Benchmark run wasn't success: " + out)
        return False


def insolar_is_alive_on_pod(pod):
    out = ssh_output(pod, 'pidof insolard || true')
    return out != ''


def wait_until_insolar_is_alive(pod_ips, nodes_online, virtual_pod=-1, nattempts=10, pause_sec=5, step=""):
    min_nalive = 2
    nalive = 0
    if virtual_pod == -1:
        virtual_pod = VIRTUALS[0]
    for attempt in range(1, nattempts+1):
        wait(pause_sec)
        try:
            alive = insolar_is_alive(pod_ips, virtual_pod, nodes_online)
            if alive:
                nalive += 1
            info("[Step: "+step+"] Alive check passed "+str(nalive)+"/" +
                 str(min_nalive)+" (attempt "+str(attempt)+" of "+str(nattempts)+")")
        except Exception as e:
            print(e)
            info("[Step: "+step+"] Insolar is not alive yet (attempt " +
                 str(attempt)+" of "+str(nattempts)+")")
            nalive = 0
        if nalive >= min_nalive:
            break
    return nalive >= min_nalive


def start_insolar_net(nodes, pod_ips, extra_args_insolard="", step=""):
    alive = False

    for attempt in range(1, 4):
        info("Insolar net not alive, kill all for clear start")
        for node in NODES:
            kill(node, "insolard")
        info("Starting insolar net (attempt %s)" % str(attempt))
        for pod in nodes:
            start_insolard(pod, extra_args=extra_args_insolard)
        info("Check insolar net alive")
        alive = wait_until_insolar_is_alive(
            pod_ips, NODES, step=step, nattempts=10)
        if alive:
            break

    check_alive(alive)


def wait_until_insolar_is_down(nattempts=10, pause_sec=5):
    all_down = False
    for pod in NODES:
        for i in range(0, nattempts):
            if not insolar_is_alive_on_pod(pod):
                all_down = True
                break
            info('Insolard is not terminated yet at pod#'+str(pod))
            all_down = False
            wait(pause_sec)
    return all_down


def start_insolard(pod, extra_args=""):
    ssh(pod, "cd " + INSPATH + " && tmux new-session -d "+extra_args+" " +
        """\\"INSOLAR_LOG_LEVEL="""+LOG_LEVEL+""" ./bin/insolard --config """ +
        "./scripts/insolard/"+str(pod) +
        "/insolar_"+str(pod)+".yaml --heavy-genesis scripts/insolard/configs/heavy_genesis.json " +
        logto("insolard")+"""; bash\\" """)


def start_pulsard(extra_args=""):
    ssh(PULSAR, "cd " + INSPATH + """ && tmux new-session -d """ +
        extra_args+""" \\"./bin/pulsard -c pulsar.yaml """ +
        logto("pulsar") + """; bash\\" """)


def kill(pod, proc_name):
    ssh(pod, "killall -s 9 "+proc_name+" || true")


def check_ssh_is_up_on_pods():
    try:
        for pod in ALL_PODS:
            out = ssh_output(pod, "echo 1")
            if out != "1":
                return False
    except Exception as e:
        print(e)
        return False
    return True


def wait_until_ssh_is_up_on_pods():
    info("Waiting until SSH daemons are up on all nodes")
    is_up = False
    nchecks = 10
    for check in range(1, nchecks+1):
        is_up = check_ssh_is_up_on_pods()
        if is_up:
            break
        info("SSH daemons are not up yet (attempt " +
             str(check)+" of "+str(nchecks)+")")
        wait(1)
    assert(is_up)
    info("SSH daemons are up!")


def prepare_configs():
    info("Building configs based on provided templates")

    info("Replace old config-templates with new")
    run("rm -r /tmp/insolar-jepsen-configs || true")
    run("cp -r ./config-templates /tmp/insolar-jepsen-configs")
    pod_ips = k8s_get_pod_ips()

    for k in sorted(pod_ips.keys(), reverse=True):
        run("find /tmp/insolar-jepsen-configs -type f -print | grep -v .bak " +
            "| xargs sed -i.bak 's/"+k.upper()+"/"+pod_ips[k]+"/g'")


def deploy_pulsar():
    info("starting pulsar (before anything else, otherwise consensus will not be reached)")
    ssh(PULSAR, "mkdir -p "+INSPATH+"/scripts/insolard/configs/")
    scp_to(PULSAR, "/tmp/insolar-jepsen-configs/pulsar.yaml",
           INSPATH+"/pulsar.yaml")
    start_pulsard(extra_args="-s pulsard")


def deploy_insolar():
    info("copying configs and fixing certificates for discovery nodes")
    pod_ips = k8s_get_pod_ips()
    for pod in NODES:
        path = INSPATH+"/scripts/insolard/"
        pod_path = path+str(pod)
        ssh(pod, "mkdir -p "+pod_path)
        for k in pod_ips.keys():
            ssh(pod, "find "+path+" -type f -print " +
                " | grep -v .bak | xargs sed -i.bak 's/"+k.upper()+"/"+pod_ips[k]+"/g'")
        if pod == HEAVY:
            ssh(pod, "mkdir -p /tmp/heavy/tmp && mkdir -p /tmp/heavy/target && mkdir -p "+INSPATH+"/data")
            ssh(pod, "cd go/src/github.com/insolar/insolar && ./bin/backupmanager create -d ./heavy_backup")
            scp_to(pod, "/tmp/insolar-jepsen-configs/last_backup_info.json",
                   INSPATH+"/data/last_backup_info.json")
        scp_to(pod, "/tmp/insolar-jepsen-configs/insolar_" +
               str(pod)+".yaml", pod_path)
        scp_to(pod, "/tmp/insolar-jepsen-configs/pulsewatcher.yaml",
               INSPATH+"/pulsewatcher.yaml")

    start_insolar_net(NODES, pod_ips, step="starting")
    info("==== Insolar started! ====")


def test_stop_start_virtuals_min_roles_ok(virtual_pods, pod_ips):
    virtual_pods_indexes = ""
    for pod in virtual_pods:
        virtual_pods_indexes = virtual_pods_indexes + str(pod) + "_"

    start_test(virtual_pods_indexes + "test_stop_start_virtuals_min_roles_ok")
    info("==== start/stop virtual at pods #" +
         virtual_pods_indexes+" test started ====")
    if len(VIRTUALS) - len(virtual_pods) < MIN_ROLES_VIRTUAL:
        msg = "TEST FAILED: test receive wrong parameter: " +\
              "amount of working virtual nodes must be more or equel to min roles in config (2 at the moment)"
        fail_test(msg)

    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-killing-virtual")
    check_alive(alive)

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-s --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    for pod in virtual_pods:
        info("Killing virtual on pod #"+str(pod))
        kill(pod, "insolard")

    alive_pod = [p for p in VIRTUALS if p not in virtual_pods][0]
    stay_alive_nods = [p for p in NODES if p not in virtual_pods]
    alive = wait_until_insolar_is_alive(
        pod_ips, stay_alive_nods, virtual_pod=alive_pod, step="virtual-down")
    check_alive(alive)

    info("Insolar is still alive. Re-launching insolard on pods #"+str(virtual_pods))
    for pod in virtual_pods:
        start_insolard(pod)

    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="virtual-up")
    check_alive(alive)

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-m --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    info("==== start/stop virtual at pods #"+str(virtual_pods)+" passed! ====")
    stop_test()


def test_stop_start_virtuals_min_roles_not_ok(virtual_pods, pod_ips):
    virtual_pods_indexes = ""
    for pod in virtual_pods:
        virtual_pods_indexes = virtual_pods_indexes + str(pod) + "_"

    start_test(virtual_pods_indexes +
               "test_stop_start_virtuals_min_roles_not_ok")
    info("==== start/stop virtual at pods #" +
         virtual_pods_indexes+" test started ====")
    if len(VIRTUALS) - len(virtual_pods) >= MIN_ROLES_VIRTUAL:
        msg = "TEST FAILED: test receive wrong parameter: " +\
            "amount of working virtual nodes must be less then min roles in config (2 at the moment)"
        fail_test(msg)

    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-killing-virtual")
    check_alive(alive)

    ok, bench_out = run_benchmark(
        pod_ips, api_pod=LIGHTS[0], extra_args='-s --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    for pod in virtual_pods:
        info("Killing virtual on pod #"+str(pod))
        kill(pod, "insolard")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down. Re-launching nodes")
    start_insolar_net(NODES, pod_ips, step="virtual-up")

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-m --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    info("==== start/stop virtual at pods #"+str(virtual_pods)+" passed! ====")
    stop_test()


def test_stop_start_lights(light_pods, pod_ips):
    light_pods_indexes = ""
    for pod in light_pods:
        light_pods_indexes = light_pods_indexes + str(pod) + "_"

    start_test(light_pods_indexes + "test_stop_start_light")
    info("==== start/stop light at pods #" +
         light_pods_indexes+" test started ====")
    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-killing-light")
    check_alive(alive)

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-s --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    info("Wait for data to save on heavy (top sync pulse must change)")
    pulse = current_pulse()
    finalized_pulse = get_finalized_pulse_from_exporter()
    while pulse != finalized_pulse:
        wait(1)
        finalized_pulse = get_finalized_pulse_from_exporter()

    info("Data was saved on heavy (top sync pulse changed)")

    for pod in light_pods:
        info("Killing light on pod #"+str(pod))
        kill(pod, "insolard")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down. Re-launching nodes")
    start_insolar_net(NODES, pod_ips, step="light-up")

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-m --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    info("==== start/stop light at pods #"+str(light_pods)+" passed! ====")
    stop_test()


def test_stop_start_heavy(heavy_pod, pod_ips, restore_from_backup=False):
    start_test("test_stop_start_heavy" +
               ("_restore_from_backup" if restore_from_backup else ""))
    info("==== start/stop heavy at pod #"+str(heavy_pod) +
         (" with restore from backup" if restore_from_backup else "")+" test started ====")
    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-killing-heavy")
    check_alive(alive)

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-s --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    info("Wait for data to save on heavy (top sync pulse must change)")
    pulse = current_pulse()
    finalized_pulse = get_finalized_pulse_from_exporter()
    while pulse != finalized_pulse:
        wait(1)
        finalized_pulse = get_finalized_pulse_from_exporter()

    info("Data was saved on heavy (top sync pulse changed)")

    info("Killing heavy on pod #"+str(heavy_pod))
    kill(heavy_pod, "insolard")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down")
    if restore_from_backup:
        info("Restoring heavy from backup...")
        ssh(heavy_pod, "cd go/src/github.com/insolar/insolar/ && " +
            "./bin/backupmanager prepare_backup -d ./heavy_backup/ -l last_backup_info.json && " +
            "rm -r data && cp -r heavy_backup data")
    info("Re-launching nodes")
    start_insolar_net(NODES, pod_ips, step="heavy-up")

    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-m --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)

    info("==== start/stop heavy at pod #"+str(heavy_pod) +
         (" with restore from backup" if restore_from_backup else "")+" passed! ====")
    stop_test()


def test_kill_heavy_under_load(heavy_pod, pod_ips, restore_from_backup=False):
    start_test("test_kill_heavy_under_load" +
               ("_restore_from_backup" if restore_from_backup else ""))
    info("==== kill heavy under load at pod #"+str(heavy_pod) +
         (" with restore from backup" if restore_from_backup else "")+" test started ====")
    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-killing-heavy")
    check_alive(alive)

    info("Starting benchmark in the background")
    run_benchmark(pod_ips, r=100, timeout=100, background=True)
    info("Killing heavy on pod #"+str(heavy_pod))
    kill(heavy_pod, "insolard")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down")
    if restore_from_backup:
        info("Restoring heavy from backup...")
        ssh(heavy_pod, "cd go/src/github.com/insolar/insolar/ && " +
            "./bin/backupmanager prepare_backup -d ./heavy_backup/ -l last_backup_info.json && " +
            "rm -r data && cp -r heavy_backup data")
    info("Re-launching nodes")
    start_insolar_net(NODES, pod_ips, step="heavy-up")

    ok, bench_out = run_benchmark(pod_ips)
    check_benchmark(ok, bench_out)

    info("==== kill heavy under load at pod #"+str(heavy_pod) +
         (" with restore from backup" if restore_from_backup else "")+" passed! ====")
    stop_test()


def test_kill_backupmanager(heavy_pod, pod_ips, restore_from_backup=False):
    start_test("test_kill_backupmanager" +
               ("_restore_from_backup" if restore_from_backup else ""))
    info("==== kill backupmanager " +
         ("with restore from backup " if restore_from_backup else "")+"test started ====")
    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-killing-backupmanager")
    check_alive(alive)

    info("Running benchmark and trying to kill backupmanager on pod #"+str(heavy_pod))
    ssh(heavy_pod, "tmux new-session -d -s backupmanager-killer " +
        """\\"while true; do killall -9 -r backupmanager; sleep 0.1; done""" +
        """; bash\\" """)
    ok, bench_out = run_benchmark(pod_ips, r=100, timeout=100)
    check(not ok, "Benchmark should fail while killing backupmanager (increase -c or -r?), but it was successfull:\n" + bench_out)

    info("Shutting down backupmanager-killer")
    ssh(heavy_pod, "tmux kill-session -t backupmanager-killer")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down")
    if restore_from_backup:
        info("Restoring heavy from backup...")
        ssh(heavy_pod, "cd go/src/github.com/insolar/insolar/ && " +
            "./bin/backupmanager prepare_backup -d ./heavy_backup/ -l last_backup_info.json && " +
            "rm -r data && cp -r heavy_backup data")
    info("Re-launching nodes")
    start_insolar_net(NODES, pod_ips, step="heavy-up")

    ok, bench_out = run_benchmark(pod_ips)
    check_benchmark(ok, bench_out)

    info("==== kill backupmanager " +
         ("with restore from backup " if restore_from_backup else "")+"passed! ====")
    stop_test()


def test_network_slow_down_speed_up(pod_ips):
    start_test("test_network_slow_down_speed_up")
    info("==== slow down / speed up network test started ====")
    for pod in ALL_PODS:
        set_network_speed(pod, SLOW_NETWORK_SPEED)
    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="slow-network")
    check_alive(alive)
    for pod in ALL_PODS:
        set_network_speed(pod, FAST_NETWORK_SPEED)
    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="fast-network")
    check_alive(alive)
    info("==== slow down / speed up network test passed! ====")
    stop_test()


def test_virtuals_slow_down_speed_up(pod_ips):
    start_test("test_virtuals_slow_down_speed_up")
    info("==== slow down / speed up virtuals test started ====")
    for pod in VIRTUALS:
        set_network_speed(pod, SLOW_NETWORK_SPEED)
    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="slow-virtuals")
    check_alive(alive)
    for pod in VIRTUALS:
        set_network_speed(pod, FAST_NETWORK_SPEED)
    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="fast-virtuals")
    check_alive(alive)
    info("==== slow down / speed up virtuals test passed! ====")
    stop_test()


def test_small_mtu(pod_ips):
    start_test("test_small_mtu")
    info("==== small mtu test started ====")
    for pod in ALL_PODS:
        set_mtu(pod, SMALL_MTU)
    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="small-mtu")
    check_alive(alive)
    for pod in ALL_PODS:
        set_mtu(pod, NORMAL_MTU)
    alive = wait_until_insolar_is_alive(pod_ips, NODES, step="noraml-mtu")
    check_alive(alive)
    info("==== small mtu test passed! ====")
    stop_test()


def test_stop_start_pulsar(pod_ips, test_num):
    start_test("test_stop_start_pulsar")
    info("==== start/stop pulsar test started ====")
    info("Killing pulsard")
    kill(PULSAR, "pulsard")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down. Re-launching net")

    info("Starting pulsar")
    start_pulsard()

    start_insolar_net(NODES, pod_ips, step="pulsar-up")
    info("==== start/stop pulsar test passed! ====")
    stop_test()


def test_netsplit_single_virtual(pod, pod_ips):
    start_test("test_netsplit_single_virtual")
    info("==== netsplit of single virtual at pod#"+str(pod)+" test started ====")
    alive_pod = [p for p in VIRTUALS if p != pod][0]
    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, step="before-netsplit-virtual")
    check_alive(alive)
    info("Emulating netsplit that affects single pod #" +
         str(pod)+", testing from pod #"+str(alive_pod))
    create_simple_netsplit(pod, pod_ips)
    stay_alive_nods = NODES.copy()
    stay_alive_nods.remove(pod)
    alive = wait_until_insolar_is_alive(
        pod_ips, stay_alive_nods, virtual_pod=alive_pod, step="netsplit-virtual")
    check_alive(alive)
    info("Insolar is alive during netsplit")
    # insolard suppose to die in case of netsplit
    for i in range(0, 10):
        if not insolar_is_alive_on_pod(pod):
            break
        info('Insolard is not terminated yet at pod#'+str(pod))
        wait(10)
    check(not insolar_is_alive_on_pod(pod),
          "Insolar must be down on pod %s, but its up" % pod)
    info('Fixing netsplit')
    fix_simple_netsplit(pod, pod_ips)
    info('Restarting insolard at pod#'+str(pod))
    start_insolard(pod)
    alive = wait_until_insolar_is_alive(
        pod_ips, NODES, virtual_pod=alive_pod, step="netsplit-virtual-relaunched")
    check_alive(alive)
    info("==== netsplit of single virtual at pod#"+str(pod)+" test passed! ====")
    stop_test()


def clear_logs_after_repetition_and_restart():
    info("Stop nodes and clear logs before next repetition")
    for node in NODES:
        kill(node, "insolard")
    kill(PULSAR, "pulsard")

    down = wait_until_insolar_is_down()
    check_down(down)
    info("Insolar is down")
    info("Clear logs before next repetition")
    for pod in NODES:
        ssh(pod, "cd " + INSPATH + " && rm insolard_*.log.gz")

    info("Starting pulsar for next repetition")
    start_pulsard()
    info("Re-launching nodes for next repetition")
    start_insolar_net(NODES, pod_ips)
    ok, bench_out = run_benchmark(
        pod_ips, extra_args='-m --members-file=' + MEMBERS_FILE)
    check_benchmark(ok, bench_out)


# check_abandoned_requests calculates abandoned requests leak.
#
# nattempts - number of attempts for checking abandoned requests metric from nodes.
# step - time in seconds between two attempts.
# verbose - flag for additional logging.
def check_abandoned_requests_not_increasing(nattempts=5, step=15, verbose=False):
    start_test("check_abandoned_requests")
    info("==== start/stop check_abandoned_requests test started ====")

    # Dict with count of abandoned metric. (key - <node_and_metric_mane>, value - <count>).
    # Example: <10.1.0.179:insolar_requests_abandoned{role="heavy_material"} 20>,
    #          <10.1.0.180:insolar_requests_abandoned{role="light_material"} 35>,
    #          ...
    abandoned_data = {}
    # Difference of abandoned requests count between all steps.
    abandoned_delta = 0
    errors = ""

    for attempt in range(1, nattempts+1):
        time.sleep(step)
        # node id for investigations
        i = 0
        abandoned_raw_data = get_abandones_count_from_nodes()

        if len(abandoned_raw_data) == 0:
            continue

        for line in abandoned_raw_data.split("\n"):
            kv = line.split()
            if len(kv) <= 1:
                # set starting value
                kv.insert(1, 0)

            # key for abandoned_data dict, consists from <node_id:node_ip>
            node = str(i) + ":" + kv[0]
            count = int(kv[1])                 # value for abandoned_data dict.
            if node in abandoned_data and count > abandoned_data[node]:
                abandoned_delta += count - abandoned_data[node]
                errors += "Attempt: " + str(attempt) + ". Abandoned increased in " + node + \
                          ". Old:" + \
                    str(abandoned_data[node]) + \
                    ", New:" + str(count) + os.linesep

            abandoned_data[node] = count
            i += 1

        if verbose:
            info("Attempt " + str(attempt) +
                 ". Abandoned requests delta: " + str(abandoned_delta))

    # If abandoned_delta is 0
    # we assume, that all of them was processed.
    if abandoned_delta != 0:
        info(errors)
    check(abandoned_delta == 0, "Unprocessed Abandoned-requests count IS NOT ZERO.")

    info("==== start/stop check_abandoned_requests test passed! ====")
    stop_test()

# get_abandones_count_from_nodes returns list of abandoned requests metric from all nodes:
#   10.1.0.179:insolar_requests_abandoned{role="heavy_material"} 1
#   10.1.0.180:insolar_requests_abandoned{role="light_material"} 20
#   ...


def get_abandones_count_from_nodes():
    abandoned_data = ssh_output(
        HEAVY, 'cd ' + INSPATH + ' && ./jepsen-tools/collect_abandoned_metrics.py')
    debug(abandoned_data)
    return abandoned_data


def check_dependencies():
    info("Checking dependencies...")
    for d in DEPENDENCIES:
        run('which ' + d)
    info("All dependencies found.")


def upload_tools(pod, pod_ips):
    info("Uploading tools ...")
    ips = ' '.join(pod_ips.values())
    ssh(pod, "mkdir -p "+INSPATH+"/jepsen-tools/ && echo " +
        ips + " > "+INSPATH+"/jepsen-tools/pod_ips")
    scp_to(pod, "./jepsen-tools/*", INSPATH+"/jepsen-tools/")


parser = argparse.ArgumentParser(
    description='Test Insolar using Jepsen-like tests')

parser.add_argument(
    '-d', '--debug', action="store_true",
    help='enable debug output')
parser.add_argument(
    '-s', '--skip-all-tests', action="store_true",
    help='skip all tests, check only deploy procedure')
parser.add_argument(
    '-m', '--minimum-tests', action="store_true",
    help='run minimal required tests set')
parser.add_argument(
    '-r', '--repeat', metavar='N', type=int, default=1,
    help='number of times to repeat tests')
parser.add_argument(
    '-n', '--namespace', metavar='X', type=str, default="default",
    help='exact k8s namespace to use')
parser.add_argument(
    '-c', '--ci', action="store_true",
    help='use CI-friendly configuration')
parser.add_argument(
    '-i', '--image', metavar='IMG', type=str, required=True,
    help='Docker image to test')

args = parser.parse_args()

NAMESPACE = args.namespace
DEBUG = args.debug
start_test("prepare")
check_dependencies()

k8s_yaml = "jepsen-pods.yaml"
info("Generating "+k8s_yaml)
k8s_gen_yaml(k8s_yaml, args.image, "IfNotPresent" if args.ci else "Never")
k8s_stop_pods_if_running(k8s_yaml)
k8s_start_pods(k8s_yaml)
POD_NODES = k8s_get_pod_nodes()
wait_until_ssh_is_up_on_pods()
pod_ips = k8s_get_pod_ips()
upload_tools(HEAVY, pod_ips)

prepare_configs()
deploy_pulsar()
deploy_insolar()

stop_test()

if args.skip_all_tests:
    notify("Deploy checked, skipping all tests")
    sys.exit(0)

ok, bench_out = run_benchmark(
    pod_ips, extra_args="-s --members-file=" + OLD_MEMBERS_FILE)
check_benchmark(ok, bench_out)
members_creted_at = time.time()
info("Wait for data to save on heavy (top sync pulse must change)")
pulse_when_members_created = current_pulse()
finalized_pulse = get_finalized_pulse_from_exporter()
while pulse_when_members_created != finalized_pulse:
    wait(1)
    finalized_pulse = get_finalized_pulse_from_exporter()

info("Data was saved on heavy (top sync pulse changed)")

tests = [
    # lambda: test_network_slow_down_speed_up(pod_ips), TODO: this test hangs on CI, fix it
    # lambda: test_virtuals_slow_down_speed_up(pod_ips), TODO: this test hangs on CI, fix it
    # lambda: test_small_mtu(pod_ips), # TODO: this test hangs @ DigitalOcean, fix it
    lambda: test_stop_start_pulsar(pod_ips, test_num),
    # lambda: test_netsplit_single_virtual(VIRTUALS[0], pod_ips), # TODO: make this test pass, see INS-2125
    lambda: test_stop_start_virtuals_min_roles_ok(VIRTUALS[:1], pod_ips),
    lambda: test_stop_start_virtuals_min_roles_ok(VIRTUALS[:2], pod_ips),
    lambda: test_stop_start_virtuals_min_roles_not_ok(VIRTUALS, pod_ips),
    lambda: test_stop_start_virtuals_min_roles_not_ok(VIRTUALS[1:], pod_ips),
    lambda: test_stop_start_lights([LIGHTS[0]], pod_ips),
    lambda: test_stop_start_lights([LIGHTS[1], LIGHTS[2]], pod_ips),
    lambda: test_stop_start_lights(LIGHTS, pod_ips),
    lambda: test_stop_start_heavy(HEAVY, pod_ips),
    lambda: test_stop_start_heavy(HEAVY, pod_ips, restore_from_backup=True),
    lambda: test_kill_heavy_under_load(HEAVY, pod_ips),
    lambda: test_kill_heavy_under_load(
        HEAVY, pod_ips, restore_from_backup=True),
    lambda: test_kill_backupmanager(HEAVY, pod_ips),
    lambda: test_kill_backupmanager(HEAVY, pod_ips, restore_from_backup=True),
]


minimum_tests = [
    lambda: test_stop_start_pulsar(pod_ips, test_num),
    lambda: test_stop_start_virtuals_min_roles_ok(VIRTUALS[:1], pod_ips),
    lambda: test_stop_start_heavy(HEAVY, pod_ips),
]

for test_num in range(0, args.repeat):
    tests_to_run = minimum_tests if args.minimum_tests else tests
    random.shuffle(tests_to_run)
    for t in tests_to_run:
        t()
    check_abandoned_requests_not_increasing(verbose=True)
    info("ALL TESTS PASSED: "+str(test_num+1)+" of "+str(args.repeat))

    # The following test should be executed after the rest of the tests
    pulses_pass = (current_pulse() - pulse_when_members_created)//PULSE_DELTA
    while pulses_pass < LIGHT_CHAIN_LIMIT:
        wait(5)
        pulses_pass = (current_pulse() -
                       pulse_when_members_created)//PULSE_DELTA

    info("Make calls to members, created at the beginning: " +
         str(pulses_pass) + " pulses ago")
    ok, bench_out = run_benchmark(
        pod_ips, extra_args="-m --members-file=" + OLD_MEMBERS_FILE)
    check_benchmark(ok, bench_out)
    if test_num != args.repeat-1:
        clear_logs_after_repetition_and_restart()

notify("Test completed!")
info("Stop nodes")
for node in NODES:
    kill(node, "insolard")
kill(PULSAR, "pulsard")
