# Bundled Sounds

`deactivate.wav` is the package-bundled warning sound used before interrupting
active LiveKit voice services during service stop and restart actions.

`voice-route-transferred.wav` and `back-to-dispatch.wav` are the route
announcements a GPT-Live call plays through the agent's audio track on a
transfer and a return (`livekit_agent/route_announcements.py`); the Classic
pipeline speaks the same moments through its announcer TTS. Both are 24 kHz
mono, recorded with the macOS Samantha voice.
