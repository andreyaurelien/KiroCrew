/**
 * Aperture wire values for the session-pulse survey (SessionPulseSurveyCard.tsx).
 *
 * PROTOCOL VALUES ONLY, same category as `apps/issue-radar/lib/wireValues.ts`:
 * every string here must match Aperture's registered form template
 * (category=KiroCrew, name=SessionFeedback, version=1.0.1) byte-for-byte, or  // brand-ok: registered category id
 * ingestion 400s on a text/type mismatch against the template. They are
 * compared/sent by value against a third-party service, never chosen for
 * their meaning to an English reader, so translating either one breaks the
 * submission rather than localizing it.
 *
 * `ratingOptions` are the `responseValue`s sent for the radio question — the
 * user-visible label shown for each is a separate, fully translated string
 * from the catalog (see `RATING_LABEL_KEYS` in SessionPulseSurveyCard.tsx).
 * `ratingQuestionText` is both the wire question text AND the on-screen
 * label/aria-label, because Aperture's ingestion API requires the submitted
 * question to match verbatim what its rendering API returns — there is no
 * separate translatable "display" copy for this one string. It shows a
 * self-hosted, English-only sentence to every locale, which is the visible
 * cost of the frozen template contract, not an oversight.
 */
export const ratingOptions = ['Very Poor', 'Poor', 'Fair', 'Good', 'Excellent']

export const ratingQuestionText = 'How would you rate your experience with KiroCrew today?' // brand-ok: verbatim registered template text, ingestion 400s on mismatch
