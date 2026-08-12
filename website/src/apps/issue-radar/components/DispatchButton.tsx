/**
 * DispatchButton — the "have an agent do this issue" control in the issue-detail
 * header, beside Investigate.
 *
 * ## Why it is inert in this change
 *
 * This is the gate's UI half. The backend decides whether dispatch COULD proceed
 * (`GET /dispatch-readiness`); nothing yet runs an agent, so the control is
 * present and disabled in every state and its tooltip says which state it is in.
 * That is deliberate rather than a placeholder: the whole point of shipping the
 * gate first is that the phase which does run an agent inherits a resolved answer
 * instead of re-deriving it from a raw path, and a user who has not set a
 * checkout should learn that HERE, next to the action, rather than by dispatching
 * and being refused.
 *
 * ## Why the reason comes from the server
 *
 * "No checkout set" and "the checkout you set is gone" need different sentences,
 * and the difference is not derivable from a path string on the client: only the
 * server can stat it. So the button renders whatever `reason` it is handed and
 * owns no rule of its own. An unknown reason falls back to the not-ready copy
 * rather than claiming readiness, for the same reason the backend re-validates on
 * every read: a state we cannot classify is not a state we may treat as fine.
 */
import { useQuery } from '@tanstack/react-query'
import { Hammer } from 'lucide-react'

import { issueRadarApi, type RepoRef } from '../api'
import { repoScopeKey } from '../lib/links'
import AgentSessionButton from './AgentSessionButton'

import { i18nT } from '../../../i18n/t'

export default function DispatchButton({ repoRef }: { repoRef: RepoRef }) {
  const scopeKey = repoScopeKey(repoRef)
  const readinessQuery = useQuery({
    queryKey: ['issue-radar', 'dispatch-readiness', scopeKey],
    queryFn: () => issueRadarApi.getDispatchReadiness(repoRef),
    staleTime: 30_000,
  })
  const readiness = readinessQuery.data ?? null

  // A pending or failed lookup must not read as "ready" — same rule the
  // Investigate control applies to an unresolved record.
  const hint = !readinessQuery.isSuccess || readiness === null
    ? i18nT('apps.issueRadar.dispatch.hintChecking')
    : readiness.reason === 'no_local_path'
      ? i18nT('apps.issueRadar.dispatch.hintNoLocalPath')
      : readiness.reason === 'checkout_unusable'
        ? i18nT('apps.issueRadar.dispatch.hintCheckoutUnusable')
        : readiness.ready
          ? i18nT('apps.issueRadar.dispatch.hintReady')
          : i18nT('apps.issueRadar.dispatch.hintNotReady')

  return (
    <AgentSessionButton
      icon={Hammer}
      label={i18nT('apps.issueRadar.dispatch.workOnIt')}
      record={null}
      busy={readinessQuery.isLoading}
      error={(readinessQuery.error as Error | null) ?? null}
      // Nothing to click yet. The click lands with the phase that starts an
      // attempt, so wiring a no-op handler now would make a disabled control look
      // like a broken one.
      onClick={() => {}}
      startHint={hint}
      resumeHint={hint}
      // No record exists in this phase, so the status pill would be stuck on
      // "pending" forever — worse than showing no status at all.
      showStatus={false}
      // Present but not actionable, so it must not wear the pane's primary fill.
      subdued
      disabled
    />
  )
}
