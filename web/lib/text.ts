// Text normalisation for agent-authored prose rendered in the UI.
//
// The LLM frequently emits em/en dashes (e.g. "coverage<emdash>but"). The
// product convention is a plain hyphen with surrounding spaces, so a long
// dash anywhere collapses to " - ". Use this on any free-text the agent
// produced (thesis, reflection, notes, watcher messages) before rendering.
// Static UI copy is hyphenated at the source, so it doesn't need this at
// runtime. The character class below is the one intentional place a long
// dash appears in source (U+2014 em, U+2013 en).

const DASH_RX = /\s*[—–]\s*/g;

export function dedash(s: string): string {
  return s.replace(DASH_RX, " - ");
}
