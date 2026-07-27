const STATUS_LINE_SPLIT_RE =
  /(?:\r?\n|\\n)+|(?<=[.!?])\s*(?=(?:Checking|Double-checking|Cross-checking|Reading|Searching|Looking|Inspecting|Running|Testing|Opening|Loading|Reviewing|Scanning|Debugging|Tracing|Verifying|Working|Pulling|Extending|Fetching|Getting|Confirming|Validating|Thinking|Brainstorming|Considering|Weighing|Planning|Drafting|Picking|Choosing)\b)/i;

export function splitIntentLines(text: string): string[] {
  return text
    .replace(/\\n/g, '\n')
    .trim()
    .split(STATUS_LINE_SPLIT_RE)
    .filter(Boolean);
}
