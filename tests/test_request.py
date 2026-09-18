import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mnd.guest import PerDecisionClient
from mnd.protocol import GuestStopped


class TemporaryError(Exception):
    pass


class PermanentError(Exception):
    pass


class RequestTests(unittest.TestCase):
    def sdk(self, outcomes):
        self.closed = 0
        self.calls = 0
        owner = self

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                owner.closed += 1

            def system_one(self, **kwargs):
                owner.calls += 1
                result = outcomes.pop(0)
                if isinstance(result, Exception):
                    raise result
                return SimpleNamespace(usage=SimpleNamespace(input_tokens=12))

        return SimpleNamespace(
            TypeSafeClient=Client,
            RetryPolicy=lambda **kw: kw,
            **dict.fromkeys(
                (
                    "TypeSafeAPIConnectionError",
                    "TypeSafeAPITimeoutError",
                    "TypeSafeInternalServerError",
                    "TypeSafeRateLimitError",
                ),
                TemporaryError,
            ),
        )

    def test_temporary_failure_retries_same_request_and_closes_connections(self):
        client = PerDecisionClient()
        attempts = []
        client.on_retry = attempts.append
        with (
            patch.dict(sys.modules, typesafe_sdk=self.sdk([TemporaryError(), True])),
            patch("mnd.guest.time.sleep"),
        ):
            client.system_one(state={}, questions={})
        self.assertEqual((self.calls, self.closed), (2, 2))
        self.assertEqual(attempts, [1])
        self.assertEqual(client.input_tokens, 12)

    def test_retry_budget_is_bounded_and_permanent_errors_are_not_retried(self):
        for error, count in ((TemporaryError, 3), (PermanentError, 1)):
            with (
                patch.dict(sys.modules, typesafe_sdk=self.sdk([error()] * 3)),
                patch("mnd.guest.time.sleep"),
                self.assertRaises(error),
            ):
                PerDecisionClient().system_one(state={}, questions={})
            self.assertEqual((self.calls, self.closed), (count, count))

    def test_cancellation_interrupts_retry_backoff(self):
        client = PerDecisionClient()
        client.on_retry = lambda _: setattr(client, "should_stop", lambda: True)
        with (
            patch.dict(sys.modules, typesafe_sdk=self.sdk([TemporaryError()])),
            self.assertRaises(GuestStopped),
        ):
            client.system_one(state={}, questions={})
        self.assertEqual((self.calls, self.closed), (1, 1))
