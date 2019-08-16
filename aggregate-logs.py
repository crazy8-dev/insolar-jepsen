#!/usr/bin/env python3

import sys
import subprocess

START_PORT = 32001
END_PORT = 32006
DEBUG = True

def debug(msg):
    if not DEBUG:
        return
    print("    "+msg)

def run(cmd):
    debug(cmd)
    code = subprocess.call(cmd, shell=True)
    if code != 0:
        print("Command `%s` returned non-zero status: %d" %
              (cmd, code))
        sys.exit(1)

if len(sys.argv) < 2:
    print("Usage: {} <copy_to_directory>".format(sys.argv[0]), file=sys.stderr)
    sys.exit(1)

copy_to_dir = sys.argv[1] + "/"

for port in range(START_PORT, END_PORT+1):
    node_dir = copy_to_dir + str(port) + "/"
    run("""mkdir -p """+node_dir+""" || true """)
    run("""scp -o 'StrictHostKeyChecking no' -i ./base-image/id_rsa -P """+str(port)+\
        """ gopher@localhost:go/src/github.com/insolar/insolar/*.log.gz """+node_dir+""" 2>/dev/null """)
