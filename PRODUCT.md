# Product

## Register

product

## Users

buzz serves self-hosting media operators and media users who want a compact interface for Real-Debrid-backed libraries. Operators use it while setting up or maintaining Jellyfin/Plex integration, checking WebDAV exposure, reviewing logs, editing configuration, and validating that fragile mount or hoster states will not damage their media library. Users may also treat buzz as an alternate Real-Debrid interface for browsing available content and understanding what the system can expose.

The common usage context is practical and local: a home-server admin console, often opened while something is being configured, verified, or debugged. The UI should assume technically capable users who appreciate directness, dense information, and predictable controls.

## Product Purpose

buzz is a small Debrid WebDAV service that safely bridges Real-Debrid content into media-server libraries. It exists to make unreliable upstream mounts safer for Jellyfin/Plex by exposing content, curating library structure, protecting against transient emptiness, and giving users a live web UI for configuration, status, logs, archive, and cache operations.

Success means the user feels in control without being slowed down: they can understand current state, locate content or settings, make changes confidently, and recover from issues without leaving the interface or interpreting ornamental UI.

## Brand Personality

Simple, low-tech, and terminal-native. buzz should feel like a focused local tool rather than a polished cloud product. Its personality borrows from terminal UIs and IDEs with a Dracula-inspired mood: compact, colorful where color carries meaning, technically honest, and calm under failure.

The voice should be direct and self-guiding. Labels and messages should explain what is happening without marketing language, exaggerated reassurance, or decorative personality.

## Anti-references

buzz should not look or behave like flashy SaaS, a gamified media app, a mobile-first consumer product, or a cloud-account dashboard. Avoid interfaces that hide raw configuration details behind opaque wizards, force pop-ups as the primary interaction model, or require heavy JavaScript before basic controls work.

Avoid repaint-heavy behavior that shifts the UI around during updates. Avoid animations, decorative transitions, layout jumps, intrusive notifications, and any front-end complexity that makes the interface slower, less predictable, or harder to maintain.

## Design Principles

1. Preserve operator confidence: keep system state, logs, configuration, and consequences visible enough that users can reason about what buzz is doing.
2. Be compact without becoming cryptic: prioritize dense information and short labels, but make the next action discoverable from the interface itself.
3. Prefer stable surfaces over spectacle: live updates should not move controls, erase context, or demand attention unless action is required.
4. Let color carry structure: use the Dracula-inspired palette to distinguish status, syntax, categories, and affordances, never as empty decoration.
5. Keep the frontend humble: basic navigation, configuration, and inspection should remain fast, resilient, and understandable with minimal JavaScript.

## Accessibility & Inclusion

The UI should be mobile-friendly and compact while remaining usable on desktop admin setups. Controls should be keyboard-friendly, labels and status text should be screen-reader friendly, and important states should never be communicated by color alone.

Dracula-inspired colors must maintain readable contrast for body text, controls, logs, and status indicators. Motion should be absent or minimal, with reduced-motion preferences respected. Layout should avoid unexpected shifts during polling or refreshes so users can read, select, and interact without losing their place.
