"""Browser WebRTC media must not be advertised through a Mihomo Fake-IP adapter."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from control.app import main
except ImportError:  # control-plane deps absent in source-only environments
    main = None


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def rewrite_sdp(sdp: str, media_host: str) -> str:
    script = r"""
import { rewriteLocalSdpForMediaHost } from './webui/src/media-sdp.js'
let source = ''
for await (const chunk of process.stdin) source += chunk
const input = JSON.parse(source)
process.stdout.write(JSON.stringify(rewriteLocalSdpForMediaHost(input.sdp, input.mediaHost)))
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "--eval", script],
        cwd=ROOT,
        input=json.dumps({"sdp": sdp, "mediaHost": media_host}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=15,
    )
    return json.loads(completed.stdout)


def release_audio_sink(owned: bool) -> list[str]:
    script = r"""
import { releaseAudioSink } from './webui/src/audio-sink.js'
const events = []
const sink = {
  srcObject: { id: 'remote-stream' },
  pause() { events.push('pause') },
  remove() { events.push('remove') },
}
releaseAudioSink(sink, JSON.parse(process.argv[1]))
if (sink.srcObject === null) events.push('cleared')
process.stdout.write(JSON.stringify(events))
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "--eval", script, json.dumps(owned)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=15,
    )
    return json.loads(completed.stdout)


@unittest.skipUnless(NODE, "Node.js is required for the WebRTC SDP unit tests")
class BrowserMediaSdpTests(unittest.TestCase):
    SDP = "\r\n".join((
        "v=0",
        "o=- 8331576794482274456 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "m=audio 61069 UDP/TLS/RTP/SAVPF 111 0 8",
        "c=IN IP4 198.18.0.1",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=candidate:1224838067 1 udp 2122260223 198.18.0.1 61069 typ host generation 0 network-id 2 network-cost 50",
        "a=candidate:4111013167 1 udp 2122194687 172.19.192.1 61070 typ host generation 0 network-id 6",
        "a=candidate:2160682161 1 udp 2121998079 192.168.137.1 61071 typ host generation 0 network-id 1 network-cost 10",
        "a=candidate:757659599 1 udp 2121932543 192.168.101.50 61072 typ host generation 0 network-id 3 network-cost 10",
        "a=candidate:936274219 1 tcp 1518280447 198.18.0.1 9 typ host tcptype active generation 0 network-id 2 network-cost 50",
        "a=ice-ufrag:yxRx",
        "a=sendrecv",
        "",
    ))

    def test_exact_published_host_replaces_fake_default_address_and_port(self):
        result = rewrite_sdp(self.SDP, "192.168.101.50")
        self.assertIn("m=audio 61072 UDP/TLS/RTP/SAVPF 111 0 8\r\n", result)
        self.assertIn("c=IN IP4 192.168.101.50\r\n", result)
        self.assertNotIn("198.18.0.1", result)
        self.assertIn("192.168.137.1 61071 typ host", result)

    def test_remote_browser_uses_a_candidate_on_the_published_host_lan(self):
        remote = self.SDP.replace("192.168.101.50 61072", "192.168.101.77 61072")
        result = rewrite_sdp(remote, "192.168.101.50")
        self.assertIn("m=audio 61072 UDP/TLS/RTP/SAVPF", result)
        self.assertIn("c=IN IP4 192.168.101.77", result)

    def test_sdp_without_the_proxy_range_is_unchanged(self):
        ordinary = self.SDP.replace("198.18.0.1", "192.168.101.50").replace("61069", "61072")
        self.assertEqual(rewrite_sdp(ordinary, "192.168.101.50"), ordinary)

    def test_react_owned_audio_sink_is_cleared_but_not_removed(self):
        self.assertEqual(release_audio_sink(False), ["pause", "cleared"])

    def test_wrapper_owned_fallback_audio_sink_is_removed(self):
        self.assertEqual(release_audio_sink(True), ["pause", "remove", "cleared"])


@unittest.skipIf(main is None, "control plane dependencies are not installed")
class SoftphoneProvisioningTests(unittest.TestCase):
    def test_provisioning_publishes_the_engine_media_host(self):
        instance = {
            "mcc": "234",
            "mnc": "33",
            "ports": {"webrtc": 8109},
            "sip": {"webrtc": {"enable": True, "username": "webrtc", "password": "secret"}},
        }
        request = SimpleNamespace(
            headers={"host": "127.0.0.1:8443"},
            url=SimpleNamespace(hostname="127.0.0.1"),
        )
        with patch.object(main.cfg, "get_instance", return_value=instance), \
                patch.object(main.cfg, "get_settings", return_value={"advertise_address": ""}), \
                patch.object(main.cfg, "ice_advertise_address", return_value="192.168.101.50"):
            result = main.api_softphone("2", request)
        self.assertEqual(result["media_host"], "192.168.101.50")
        self.assertEqual(result["ws_port"], 8109)


if __name__ == "__main__":
    unittest.main()
