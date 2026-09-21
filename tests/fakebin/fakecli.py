#!/usr/bin/env python3
"""A fake AI command-line tool for offline tests. The first argument picks the behaviour."""
import hashlib
import os
import subprocess
import sys
import time

mode = sys.argv[1]
args = sys.argv[2:]
counter = os.environ.get("FAKECLI_COUNTER")
if counter:
    with open(counter, "a") as f:
        f.write(mode + "\n")


def say(text):
    sys.stdout.write(text)
    sys.stdout.flush()


if mode == "ok":
    say("Canberra is the capital of Australia. Parliament first sat there in 1927. args=" + " ".join(args) + "\n")
elif mode == "ok2":
    say("The capital of Australia is Canberra; Parliament moved there in 1927 from Melbourne.\n")
elif mode == "slow":
    say("This is the first part of a long answer that never finishes in time.\n")
    time.sleep(60)
    say("never reached\n")
elif mode == "stdin":
    data = sys.stdin.read()
    say("I did not hang: stdin gave me %d bytes, and here is a full answer sentence.\n" % len(data))
elif mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKECLI_PIDFILE"], "w") as f:
        f.write(str(child.pid))
    say("started a helper process and now I wait for it forever.\n")
    time.sleep(120)
elif mode == "leaver":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKECLI_PIDFILE"], "w") as f:
        f.write(str(child.pid))
    say("I answered and left a helper process running behind me, which must not survive.\n")
elif mode == "ratelimit":
    say("An API rate limit caps how many requests you may send per minute. Going over the rate limit returns 429.\n")
elif mode == "header":
    sys.stderr.write("OpenAI-style header\nmodel: real-model-7\nreasoning effort: low\n")
    say("Here is a complete answer from a tool that reports its real model in a header.\n")
elif mode == "quota":
    sys.stderr.write("Error: 403 You've reached your weekly (7-day) usage limit\n")
    sys.exit(1)
elif mode == "auth":
    sys.stderr.write("Error: not logged in. Please run the login command.\n")
    sys.exit(1)
elif mode == "huge":
    chunk = ("x" * 1023 + "\n") * 1024
    for _ in range(4):
        say(chunk)
    time.sleep(30)
elif mode == "sha":
    data = sys.stdin.buffer.read()
    say("sha256 of what I received: " + hashlib.sha256(data).hexdigest() + "\n")
elif mode == "env":
    say("OPENAI_API_KEY=" + (os.environ.get("OPENAI_API_KEY") or "EMPTY") + " and that is all I can see here.\n")
elif mode == "empty":
    pass
elif mode == "file":
    path = args[args.index("--prompt-file") + 1]
    say("I read the prompt file: " + open(path).read()[:40].replace("\n", " ") + " ... done.\n")
else:
    sys.exit("unknown mode " + mode)
