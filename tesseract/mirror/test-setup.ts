// jsdom gaps that would otherwise decide which components can be tested.
//
// `pdfjs-dist` reads `DOMMatrix` at import time, and jsdom has no such class.
// Anything that transitively imports the PDF preview then fails to COLLECT,
// which is worse than a failing assertion: the whole file reports zero tests.
// That reaches further than the PDF preview itself, because the chat message
// bubble renders attachments, so the HUD chat widget inherits it.
//
// A minimal stand-in is enough. No test here renders a PDF; they only need the
// import to survive.
if (!('DOMMatrix' in globalThis)) {
  class DOMMatrixStub {
    a = 1;
    b = 0;
    c = 0;
    d = 1;
    e = 0;
    f = 0;
    constructor(_init?: unknown) {}
    translate() {
      return this;
    }
    scale() {
      return this;
    }
    multiply() {
      return this;
    }
  }
  Object.defineProperty(globalThis, 'DOMMatrix', {
    value: DOMMatrixStub,
    writable: true,
    configurable: true,
  });
}
