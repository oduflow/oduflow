"""A one-build owner for a short-lived `oduflow call` process."""

import json
import os
import socket
import subprocess
import sys
from typing import Any

from oduflow.errors import FlowError


def start(kwargs: dict[str, Any], *, no_telemetry: bool) -> str:
    # A private socket carries the arguments (possibly a private repo URL)
    # and the admission reply. No terminal or caller pipe stays with the job.
    caller, worker = socket.socketpair()
    with caller, worker:
        fd = worker.fileno()
        proc = subprocess.Popen(
            [sys.executable, "-m", "oduflow.cli_build", str(fd)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(fd,),
            start_new_session=True,
        )
        worker.close()
        caller.sendall(json.dumps([kwargs, no_telemetry]).encode())
        caller.shutdown(socket.SHUT_WR)
        with caller.makefile("rb") as stream:
            reply = stream.read()
        if not reply:
            raise FlowError(
                f"Build worker exited before admission (exit {proc.wait()})."
            )
        ok, message = json.loads(reply)
        if not ok:
            proc.wait()
            raise FlowError(message)
        return str(message)


def _work(fd: int) -> None:
    os.set_inheritable(fd, False)
    from oduflow import image_builds, server
    from oduflow.docker_ops import build_ops
    from oduflow.naming import redact_url_credentials

    with socket.socket(fileno=fd) as caller:
        with caller.makefile("rb") as stream:
            kwargs, server._no_telemetry = json.load(stream)
        try:
            team, cfg, job, args, remote = server._admit_image_build(**kwargs)
        except Exception as exc:
            caller.sendall(
                json.dumps([False, redact_url_credentials(str(exc))]).encode()
            )
            return
        try:
            caller.sendall(
                json.dumps([True, server._image_build_started(cfg, job)]).encode()
            )
        except BrokenPipeError:
            # The caller left during admission; there is nobody to return an ID to.
            image_builds.store.finish(
                team, job, image_builds.STATUS_CANCELLED, "Caller disconnected."
            )
            return
    build_ops.run_build(team, cfg, job, args, remote)


if __name__ == "__main__":
    _work(int(sys.argv[1]))
