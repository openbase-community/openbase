import threading

from openbase_coder_cli import stack_sampler


def _busy(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(range(1000))


def test_sampler_attributes_busy_thread(tmp_path) -> None:
    stop = threading.Event()
    worker = threading.Thread(target=_busy, args=(stop,), daemon=True)
    worker.start()
    try:
        counts, samples = stack_sampler.sample_stacks(duration=0.3, interval=0.01)
    finally:
        stop.set()
        worker.join()
    assert samples > 5
    assert any("in _busy" in key for key in counts)
