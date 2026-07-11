"""Offline tests for voice.py — stubs the Whisper call, no model/network.
Run: python test_voice.py"""
import asyncio

import voice


async def main():
    # --- happy path: the async wrapper returns the transcription verbatim ---
    voice._transcribe_sync = lambda p: "set the volume to twenty percent"
    assert (await voice.transcribe("clip.ogg")) == "set the volume to twenty percent"
    print("PASS transcribe returns text (stubbed)")

    # --- errors are swallowed into a string (the wrapper never raises) ---
    def boom(path):
        raise RuntimeError("faster-whisper not installed")
    voice._transcribe_sync = boom
    r = await voice.transcribe("clip.ogg")
    assert r.startswith("Error") and "faster-whisper" in r, r
    print("PASS transcribe swallows errors into 'Error: ...'")

    # --- empty audio -> empty string (bridge treats this as 'no speech') ---
    voice._transcribe_sync = lambda p: ""
    assert (await voice.transcribe("clip.ogg")) == ""
    print("PASS transcribe passes through empty result")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
