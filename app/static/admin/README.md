# Admin console scripts

The admin console uses ordered classic scripts so the existing shared state and
function bindings remain compatible without a bundler or changes to backend
routes. `admin.html` is the single source of truth for load order:

1. `core.js` — shared state, formatting, navigation, authentication, and API client.
2. `overview.js` — dashboard metrics, capacity, analytics, and runtime polling.
3. `resources.js` — account, user, invite, and generation-log tables.
4. `preview.js` — log preview plus dashboard/resource refresh orchestration.
5. `settings.js` — retention, storage, and runtime settings.
6. `dialogs.js` — account, user, invite dialogs, imports, and table filters.
7. `bootstrap.js` — DOM event binding and application startup.

Keep declarations at top level and preserve this dependency order unless the
console is migrated to native modules or a bundled module graph. New behavior
belongs in the narrowest feature file; cross-feature primitives belong in
`core.js`.
