// Which panels the shell offers a viewer (agent_system/ui/views.py): admins every panel their role may open, everyone
// else the ones the admins released in the User view panel. Only the launcher and the palette list by it; the chat
// stays as it is, and a panel opened by its address or restored from the layout opens as before.
//   GET /api/ui/view -> {role, mode, can_switch, panels: [ids] | null}
import { api, html, icon } from '/static/kit/panel-kit.js';

/** What an admin is offered when the server cannot say: every panel, as before there was a release. */
export const FULL_VIEW = Object.freeze({ role: 'admin', panels: null });

export async function loadView(user = null) {
  try {
    return await api('/api/ui/view', { quiet: true });
  } catch {
    if (!user || user.role === 'admin') return { ...FULL_VIEW };
    return { role: user.role, panels: null };
  }
}

/** The released panels from the catalogue, in the order the admins chose; every panel where there is no release. */
export function shownPanels(catalog, view) {
  if (!view?.panels) return catalog.panels;
  return view.panels.map((id) => catalog.panels.find((panel) => panel.id === id)).filter(Boolean);
}

/** The catalogue as the launcher lists it: the same categories, only the released panels. */
export const shownCatalog = (catalog, view) => ({ ...catalog, panels: shownPanels(catalog, view) });

/** The account menu's entry: admins open the User view panel, others are told who chose their panels. */
export function viewMenu(view) {
  if (view.role === 'admin') {
    return html`<button type="button" class="pk-menu-item" data-open-panel="user_view">${icon('users')} User view…</button>`;
  }
  return html`<span class="app-menu-note" data-view-note>Your panels are released by your admins. Missing one? Ask them.</span>`;
}
