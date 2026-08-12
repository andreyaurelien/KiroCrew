import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import FilePathMenu, { FilePathMenuItems } from '../components/FilePathMenu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent } from '../components/ui/context-menu'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent } from '../components/ui/dropdown-menu'

// ── Mocks ────────────────────────────────────────────────────────────────────

const brandingEnv = vi.hoisted(() => ({ directLocal: true }))

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

vi.mock('../api/client', () => ({
  api: {
    revealPath: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(),
}))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'

// ── Helpers ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks()
  brandingEnv.directLocal = true
})

afterEach(() => {
  brandingEnv.directLocal = true
})

function rightClick(el: Element) {
  fireEvent.contextMenu(el)
}

// ── FilePathMenu (right-click wrapper) ───────────────────────────────────────

describe('FilePathMenu', () => {
  const TEST_PATH = '/home/user/project/report.md'

  describe('when directLocal is true', () => {
    it('renders all three items: open, reveal, copy path', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      expect(screen.getByText('Copy path')).toBeInTheDocument()
    })

    it('calls revealPath with "open" when Open item is selected', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Open with default app'))

      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'open')
      })
    })

    it('calls revealPath with "reveal" when Reveal item is selected', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Show in file manager'))

      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'reveal')
      })
    })

    it('calls copyToClipboard when Copy path item is selected', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Copy path'))

      expect(copyToClipboard).toHaveBeenCalledWith(TEST_PATH)
    })
  })

  describe('when directLocal is false (remote session)', () => {
    beforeEach(() => { brandingEnv.directLocal = false })

    it('hides open and reveal items, shows only copy path', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      expect(screen.queryByText('Open with default app')).not.toBeInTheDocument()
      expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
    })
  })

  describe('with directLocal prop override', () => {
    beforeEach(() => { brandingEnv.directLocal = false })

    it('respects the explicit directLocal prop over branding', async () => {
      renderWithProviders(
        <FilePathMenu filePath={TEST_PATH} directLocal={true}>
          <span data-testid="trigger">report.md</span>
        </FilePathMenu>,
      )

      rightClick(screen.getByTestId('trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      expect(screen.getByText('Copy path')).toBeInTheDocument()
    })
  })
})

// ── FilePathMenuItems (reusable building blocks) ─────────────────────────────

describe('FilePathMenuItems', () => {
  const TEST_PATH = '/tmp/demo.html'

  describe('variant="dropdown"', () => {
    function renderDropdown(props: { directLocal?: boolean; leadingSeparator?: boolean }) {
      return renderWithProviders(
        <DropdownMenu defaultOpen>
          <DropdownMenuTrigger>Menu</DropdownMenuTrigger>
          <DropdownMenuContent>
            <FilePathMenuItems
              filePath={TEST_PATH}
              variant="dropdown"
              directLocal={props.directLocal}
              leadingSeparator={props.leadingSeparator}
            />
          </DropdownMenuContent>
        </DropdownMenu>,
      )
    }

    it('renders all items when directLocal', async () => {
      renderDropdown({ directLocal: true })

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      expect(screen.getByText('Copy path')).toBeInTheDocument()
    })

    it('renders only copy path when not directLocal', async () => {
      renderDropdown({ directLocal: false })

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      expect(screen.queryByText('Open with default app')).not.toBeInTheDocument()
      expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
    })

    it('calls revealPath("open") on open item click', async () => {
      renderDropdown({ directLocal: true })

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Open with default app'))

      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'open')
      })
    })

    it('calls revealPath("reveal") on reveal item click', async () => {
      renderDropdown({ directLocal: true })

      await waitFor(() => {
        expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Show in file manager'))

      await waitFor(() => {
        expect(api.revealPath).toHaveBeenCalledWith(TEST_PATH, 'reveal')
      })
    })

    it('calls copyToClipboard on copy path click', async () => {
      renderDropdown({ directLocal: true })

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Copy path'))

      expect(copyToClipboard).toHaveBeenCalledWith(TEST_PATH)
    })
  })

  describe('variant="context"', () => {
    function renderContext(props: { directLocal?: boolean }) {
      return renderWithProviders(
        <ContextMenu>
          <ContextMenuTrigger>
            <span data-testid="ctx-trigger">file.txt</span>
          </ContextMenuTrigger>
          <ContextMenuContent>
            <FilePathMenuItems
              filePath={TEST_PATH}
              variant="context"
              directLocal={props.directLocal}
            />
          </ContextMenuContent>
        </ContextMenu>,
      )
    }

    it('renders items when directLocal', async () => {
      renderContext({ directLocal: true })

      rightClick(screen.getByTestId('ctx-trigger'))

      await waitFor(() => {
        expect(screen.getByText('Open with default app')).toBeInTheDocument()
      })
      expect(screen.getByText('Show in file manager')).toBeInTheDocument()
      expect(screen.getByText('Copy path')).toBeInTheDocument()
    })

    it('hides open/reveal when remote', async () => {
      renderContext({ directLocal: false })

      rightClick(screen.getByTestId('ctx-trigger'))

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      expect(screen.queryByText('Open with default app')).not.toBeInTheDocument()
      expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
    })
  })

  describe('onAction callback', () => {
    it('fires onAction after copy path is clicked', async () => {
      const onAction = vi.fn()
      renderWithProviders(
        <DropdownMenu defaultOpen>
          <DropdownMenuTrigger>Menu</DropdownMenuTrigger>
          <DropdownMenuContent>
            <FilePathMenuItems
              filePath="/tmp/demo.html"
              variant="dropdown"
              directLocal={true}
              onAction={onAction}
            />
          </DropdownMenuContent>
        </DropdownMenu>,
      )

      await waitFor(() => {
        expect(screen.getByText('Copy path')).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Copy path'))

      expect(onAction).toHaveBeenCalled()
    })
  })

  describe('aria labels', () => {
    it('each item has an accessible aria-label', async () => {
      renderWithProviders(
        <DropdownMenu defaultOpen>
          <DropdownMenuTrigger>Menu</DropdownMenuTrigger>
          <DropdownMenuContent>
            <FilePathMenuItems
              filePath="/tmp/demo.html"
              variant="dropdown"
              directLocal={true}
            />
          </DropdownMenuContent>
        </DropdownMenu>,
      )

      await waitFor(() => {
        expect(screen.getByLabelText('Open with default app')).toBeInTheDocument()
      })
      expect(screen.getByLabelText('Show in file manager')).toBeInTheDocument()
      expect(screen.getByLabelText('Copy path')).toBeInTheDocument()
    })
  })
})
