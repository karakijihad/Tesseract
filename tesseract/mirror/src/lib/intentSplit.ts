const STATUS_LINE_SPLIT_RE =
  /(?:\r?\n|\\n)+|(?<=[.!?])\s*(?=(?:Checking|Double-checking|Cross-checking|Reading|Searching|Looking|Inspecting|Running|Testing|Opening|Loading|Reviewing|Scanning|Debugging|Tracing|Verifying|Working|Pulling|Extending|Fetching|Getting|Confirming|Validating|Thinking|Brainstorming|Considering|Weighing|Planning|Drafting|Picking|Choosing)\b)/i;

// The intent surface renders as plain text, never through Markdown — a status
// line is not a document. The contract tells the model to emit plain prose, but
// a model is not a parser: anything that slips through would be shown to the
// operator as literal asterisks and read aloud as noise, so it is stripped here
// rather than trusted upstream.
const IMAGE_RE = /!\[[^\]]*\]\([^)]*\)/g;
const LINK_RE = /\[([^\]]+)\]\([^)]*\)/g;
const BACKTICK_RE = /`+/g;
const STRONG_RE = /(\*\*|__)(?=\S)([\s\S]+?)(?<=\S)\1/g;
const UNDERSCORE_EM_RE = /(?<![A-Za-z0-9_])_(?=\S)([^_]+?)(?<=\S)_(?![A-Za-z0-9_])/g;
const ASTERISK_RE = /\*/g;
const LEADING_MARK_RE = /^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|>[ \t]?)/;

function stripMarkup(line: string): string {
  return line
    .replace(LEADING_MARK_RE, '')
    .replace(IMAGE_RE, '')
    .replace(LINK_RE, '$1')
    .replace(BACKTICK_RE, '')
    .replace(STRONG_RE, '$2')
    .replace(UNDERSCORE_EM_RE, '$1')
    .replace(ASTERISK_RE, '')
    .replace(/[ \t]{2,}/g, ' ')
    .trim();
}

export function splitIntentLines(text: string): string[] {
  return text
    .replace(/\\n/g, '\n')
    .trim()
    .split(STATUS_LINE_SPLIT_RE)
    .filter(Boolean)
    .map(stripMarkup)
    .filter(Boolean);
}
