/**
 * Shared file-path context menu: Open in default app, Reveal in Finder / file
 * manager, Copy path. Exposed both as a right-click wrapper (`FilePathMenu`)
 * and as standalone menu-item building blocks (`FilePathMenuItems`) so dropdown
 * menus can reuse the same items and labels.
 *
 * Open/Reveal items render only when `directLocal` is true (the backend reports
 * the request comes from a browser on the same machine). Remote and tunneled
 * sessions see Copy path only, because opening Finder on a host the user is not
 * looking at is useless.
 */
import { type ReactNode } from 'react'
import { ExternalLink, FolderOpen, Copy } from 'lucide-react'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
} from './ui/context-menu'
import { DropdownMenuItem, DropdownMenuSeparator } from './ui/dropdown-menu'
import { useBranding } from '../hooks/useBranding'
import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { i18nT } from '../i18n/t'

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Read `directLocal` from the branding context. The field is added by the
 * reveal-endpoint-local-gate loop; until that ships, the hook type does not
 * include it, so we access it via a wider cast. At runtime the field is either
 * `true` or absent (→ `undefined` → coerces to `false` = safe remote default).
 */
function useDirectLocal(): boolean {
  const branding = useBranding() as { directLocal?: boolean }
  return !!branding.directLocal
}

async function revealOrOpen(filePath: string, action: 'open' | 'reveal') {
  try {
    const res = await api.revealPath(filePath, action)
    if (res?.copy) {
      alert(i18nT('components.filePathMenu.path_copied_no_desktop'))
    }
  } catch (err) {
    // eslint-disable-next-line no-console -- surface reveal failures for diagnostics
    console.error('revealPath failed', err)
    alert((err as Error).message)
  }
}

// ── Menu-item building blocks ────────────────────────────────────────────────

export type FilePathMenuVariant = 'context' | 'dropdown'

export interface FilePathMenuItemsProps {
  /** Absolute file path to act on. */
  filePath: string
  /** Selects the Radix primitive family; must match the enclosing menu. */
  variant: FilePathMenuVariant
  /** Override the directLocal flag (defaults to the branding value). Useful
   *  when the consuming component already has the value from another source. */
  directLocal?: boolean
  /** Called after any action fires — lets the caller close its menu. */
  onAction?: () => void
  /** When true, renders a leading separator before the group (use when
   *  appending to an existing menu). */
  leadingSeparator?: boolean
}

/**
 * Renders the file-path action items (Open / Reveal / Copy path) using the
 * correct Radix primitive family. Drop these into any DropdownMenuContent or
 * ContextMenuContent.
 */
export function FilePathMenuItems({
  filePath,
  variant,
  directLocal: directLocalProp,
  onAction,
  leadingSeparator,
}: FilePathMenuItemsProps) {
  const brandingLocal = useDirectLocal()
  const isLocal = directLocalProp ?? brandingLocal

  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const Separator = variant === 'context' ? ContextMenuSeparator : DropdownMenuSeparator

  return (
    <>
      {leadingSeparator && <Separator />}
      {isLocal && (
        <Item
          onSelect={() => { void revealOrOpen(filePath, 'open'); onAction?.() }}
          aria-label={i18nT('components.filePathMenu.open_with_default_app')}
        >
          <ExternalLink size={14} className="lucide-inline" />
          {i18nT('components.filePathMenu.open_with_default_app')}
        </Item>
      )}
      {isLocal && (
        <Item
          onSelect={() => { void revealOrOpen(filePath, 'reveal'); onAction?.() }}
          aria-label={i18nT('components.filePathMenu.show_in_file_manager')}
        >
          <FolderOpen size={14} className="lucide-inline" />
          {i18nT('components.filePathMenu.show_in_file_manager')}
        </Item>
      )}
      <Item
        onSelect={() => { copyToClipboard(filePath); onAction?.() }}
        aria-label={i18nT('components.filePathMenu.copy_path')}
      >
        <Copy size={14} className="lucide-inline" />
        {i18nT('components.filePathMenu.copy_path')}
      </Item>
    </>
  )
}

// ── Right-click wrapper ──────────────────────────────────────────────────────

export interface FilePathMenuProps {
  /** Absolute file path to act on. */
  filePath: string
  /** The element that triggers the context menu on right-click. */
  children: ReactNode
  /** Override the directLocal flag. */
  directLocal?: boolean
}

/**
 * Wrap any element to give it a right-click menu with file-path actions.
 *
 * ```tsx
 * <FilePathMenu filePath="/home/user/report.md">
 *   <span className="file-title">report.md</span>
 * </FilePathMenu>
 * ```
 */
export default function FilePathMenu({ filePath, children, directLocal }: FilePathMenuProps) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent className="min-w-[180px]" onClick={e => e.stopPropagation()}>
        <FilePathMenuItems
          filePath={filePath}
          variant="context"
          directLocal={directLocal}
        />
      </ContextMenuContent>
    </ContextMenu>
  )
}
