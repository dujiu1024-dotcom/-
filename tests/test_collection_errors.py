import unittest

from app.services.collection_errors import classify_collection_error


class CollectionErrorClassificationTests(unittest.TestCase):
    def test_rate_limit_is_retryable(self):
        result = classify_collection_error("HTTP 429 Too Many Requests")
        self.assertEqual(result["category"], "rate_limited")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["manual_intervention"])

    def test_authentication_requires_manual_intervention(self):
        result = classify_collection_error("登录失效，请先登录")
        self.assertEqual(result["category"], "authentication_required")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["manual_intervention"])

    def test_captcha_is_never_automatically_retried(self):
        result = classify_collection_error("检测到验证码")
        self.assertEqual(result["code"], "COLLECTION_CAPTCHA_REQUIRED")
        self.assertFalse(result["retryable"])

    def test_transient_network_errors_are_retryable(self):
        result = classify_collection_error("Connection reset by peer")
        self.assertEqual(result["category"], "network_error")
        self.assertTrue(result["retryable"])

    def test_unknown_errors_fail_closed(self):
        result = classify_collection_error("unexpected parser state")
        self.assertEqual(result["category"], "unknown")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["manual_intervention"])

    def test_message_is_bounded(self):
        result = classify_collection_error("x" * 3000)
        self.assertEqual(len(result["message"]), 2000)


if __name__ == "__main__":
    unittest.main()
