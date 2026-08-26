import unittest

from acoustic_field import AcousticField


class Clock:
    def __init__(self):
        self.ns = 0
    def __call__(self):
        return self.ns
    def ms(self, value):
        self.ns += int(value * 1_000_000)


class AcousticFieldTest(unittest.TestCase):
    def test_partial_text_can_prepare_and_emit_before_done(self):
        f = AcousticField(min_chars=3, max_phrase_chars=40)
        out = f.observe('fast answer, still growing', 1, done=False)
        self.assertEqual(out[0].kind, 'prime')
        self.assertTrue(any(x.kind == 'phrase' for x in out))
        self.assertLess(f.emitted_chars, len('fast answer, still growing'))

    def test_user_activity_suppresses_phrase_and_primes_only(self):
        f = AcousticField(min_chars=3, max_phrase_chars=30)
        f.set_user_active(True)
        out = f.observe('do not talk over me.', 1, done=False)
        self.assertTrue(out)
        self.assertTrue(all(x.kind == 'prime' for x in out))

    def test_user_onset_cuts_and_advances_generation(self):
        f = AcousticField(min_chars=3, max_phrase_chars=30)
        out = f.observe('one phrase. another phrase.', 1, done=False)
        phrase = next(x for x in out if x.kind == 'phrase')
        old_generation = phrase.generation
        cut = f.set_user_active(True)
        self.assertEqual([x.kind for x in cut], ['cut'])
        self.assertGreater(cut[0].generation, old_generation)
        self.assertEqual(cut[0].reason, 'user-onset')

    def test_user_release_has_small_silence_guard_then_resumes(self):
        clock = Clock()
        f = AcousticField(min_chars=3, max_phrase_chars=30, resume_guard_ms=64, clock_ns=clock)
        f.set_user_active(True)
        f.observe('ready phrase. tail', 1, done=False)
        f.set_user_active(False)
        self.assertFalse(any(x.kind == 'phrase' for x in f.advance()))
        clock.ms(63)
        self.assertFalse(any(x.kind == 'phrase' for x in f.advance()))
        clock.ms(1)
        self.assertTrue(any(x.kind == 'phrase' for x in f.advance()))

    def test_prime_is_delta_not_spam(self):
        f = AcousticField()
        first = f.observe('abc', 1)
        second = f.observe('abc', 1)
        third = f.observe('abcdef', 1)
        self.assertEqual([x.kind for x in first], ['prime'])
        self.assertEqual(second, [])
        self.assertEqual([x.kind for x in third], ['prime'])
        self.assertEqual((third[0].start, third[0].end), (3, 6))

    def test_same_revision_rewrite_invalidates_prepared_suffix(self):
        f = AcousticField(min_chars=3, max_phrase_chars=30)
        first = f.observe('alpha beta. stale suffix', 5, done=False)
        old_generation = first[-1].generation
        rewritten = f.observe('alpha NEW answer.', 5, done=False)
        self.assertGreater(f.generation, old_generation)
        self.assertTrue(any(x.kind == 'prime' for x in rewritten))
        self.assertLessEqual(f.emitted_chars, len('alpha NEW answer.'))

    def test_stale_revision_cannot_resurrect_old_work(self):
        f = AcousticField(min_chars=3)
        f.observe('new response.', 10, done=False)
        before = (f.generation, f.emitted_chars, f.primed_chars)
        out = f.observe('old response should not return.', 9, done=True)
        self.assertEqual(out, [])
        self.assertEqual((f.generation, f.emitted_chars, f.primed_chars), before)

    def test_long_unpunctuated_stream_forces_word_boundary_not_tiny_chunks(self):
        f = AcousticField(min_chars=4, max_phrase_chars=24)
        text = 'this is a deliberately long unpunctuated stream that keeps going'
        out = f.observe(text, 1, done=False)
        phrases = [x for x in out if x.kind == 'phrase']
        self.assertTrue(phrases)
        self.assertTrue(all((p.end - p.start) >= 12 for p in phrases))
        self.assertTrue(all(text[p.end - 1].isspace() for p in phrases))

    def test_supersede_cuts_old_response_and_starts_new_generation_cleanly(self):
        clock = Clock()
        f = AcousticField(min_chars=3, max_phrase_chars=30, clock_ns=clock)
        old = f.observe('old phrase. unfinished tail', 4, done=False)
        old_generation = next(x.generation for x in old if x.kind == 'phrase')
        cut = f.supersede()
        self.assertEqual([x.kind for x in cut], ['cut'])
        self.assertEqual(cut[0].reason, 'response-superseded')
        self.assertGreater(cut[0].generation, old_generation)
        snap = f.snapshot()
        self.assertEqual((snap['chars'], snap['primed_chars'], snap['emitted_chars']), (0, 0, 0))
        new = f.observe('new phrase.', 0, done=True)
        self.assertTrue(any(x.kind == 'phrase' for x in new))
        self.assertTrue(all(x.generation == f.generation for x in new))

    def test_supersede_while_user_active_keeps_new_response_silent(self):
        clock = Clock()
        f = AcousticField(min_chars=3, max_phrase_chars=30, resume_guard_ms=64, clock_ns=clock)
        f.observe('old phrase.', 1, done=False)
        f.set_user_active(True)
        f.supersede()
        out = f.observe('new phrase.', 0, done=True)
        self.assertTrue(out)
        self.assertTrue(all(x.kind == 'prime' for x in out))
        f.set_user_active(False)
        clock.ms(64)
        resumed = f.advance()
        self.assertTrue(any(x.kind == 'phrase' for x in resumed))

    def test_done_releases_without_voice_identity(self):
        f = AcousticField(min_chars=3, max_phrase_chars=50)
        out = f.observe('compact final', 1, done=True)
        self.assertEqual(out[-1].kind, 'release')
        self.assertFalse(any(hasattr(x, 'voice') for x in out))
        self.assertFalse(any(hasattr(x, 'samples') for x in out))


if __name__ == '__main__':
    unittest.main(verbosity=2)
