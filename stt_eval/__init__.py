"""Japanese speech-recognition measurement on Windows.

The package holds the audio pipeline (fixture loading, causal resampling, WASAPI
loopback capture), the engine adapters (a WSL NeMo worker, a hosted transcription
session, an experimental ONNX candidate), the comparison harness, the metrics,
resource sampling, and the localhost tooling used to record and review natural
speech. Nothing here writes captured audio or transcripts outside an explicit
fixture-recording session.
"""
