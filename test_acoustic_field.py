import unittest

from acoustic_field import AcousticField


class AcousticFieldTest(unittest.TestCase):
    def test_partial_text_can_emit_before_done(self):
        f = AcousticField(min_chars=3, soft_chars=8)
        out = f.observe('fast answer, still growing', 1, done=False)
        self.assertTrue(any(x.kind == 'phrase' for x in out))
        self.assertLess(f.emitted_chars, len('fast answer, still growing'))

    def test_user_activity_suppresses_phrase_and_primes_only(self):
        f = AcousticField(min_chars=3, soft_chars=6)
        f.set_user_active(True)
        out = f.observe('do not talk over me.', 1, done=False)
        self.assertTrue(out)
        self.assertTrue(all(x.kind == 'prime' for x in out))

    def test_user_onset_cuts_current_emission(self):
        f = AcousticField(min_chars=3, soft_chars=6)
        out = f.observe('one phrase. another phrase.', 1, done=False)
        self.assertTrue(any(x.kind == 'phrase' for x in out))
        cut = f.set_user_active(True)
        self.assertEqual([x.kind for x in cut], ['cut'])
        self.assertEqual(cut[0].reason, 'user-onset')

    def test_rewrite_never_continues_stale_suffix(self):
        f = AcousticField(min_chars=3, soft_chars=6)
        f.observe('alpha beta. stale suffix.', 1, done=False)
        before = f.emitted_chars
        f.observe('alpha NEW answer.', 2, done=False)
        self.assertLessEqual(f.emitted_chars, before)

    def test_done_releases_without_voice_identity(self):
        f = AcousticField(min_chars=3, soft_chars=50)
        out = f.observe('compact final', 1, done=True)
        self.assertEqual(out[-1].kind, 'release')
        self.assertFalse(any(hasattr(x, 'voice') for x in out))


if __name__ == '__main__':
    unittest.main(verbosity=2)
