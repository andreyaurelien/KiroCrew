import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'

describe('api.revealPath', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  afterEach(() => { fetchSpy.mockRestore() })

  it('sends action="reveal" by default', async () => {
    await api.revealPath('/tmp/file.txt')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/reveal')
    const body = JSON.parse(init.body as string)
    expect(body).toEqual({ path: '/tmp/file.txt', action: 'reveal' })
  })

  it('sends action="open" when specified', async () => {
    await api.revealPath('/tmp/file.txt', 'open')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/reveal')
    const body = JSON.parse(init.body as string)
    expect(body).toEqual({ path: '/tmp/file.txt', action: 'open' })
  })

  it('sends action="reveal" when explicitly passed', async () => {
    await api.revealPath('/home/user/doc.md', 'reveal')
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    const body = JSON.parse(init.body as string)
    expect(body.action).toBe('reveal')
    expect(body.path).toBe('/home/user/doc.md')
  })

  it('returns the copy field when response contains it (remote fallback)', async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true, copy: '/remote/path.txt' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const result = await api.revealPath('/remote/path.txt')
    expect(result).toEqual({ ok: true, copy: '/remote/path.txt' })
  })
})
