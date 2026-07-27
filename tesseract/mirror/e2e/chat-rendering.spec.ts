import { test, expect } from '@playwright/test';

const MOCK_API: Record<string, unknown> = {
  '/api/identity': {
    name: 'TARS',
    operator_name: 'You',
    version: 'test',
    security_mode: 'headless',
    model_role: 'chat_brain',
    model_name: 'test-model',
    provider: 'test',
    observer_model: null,
    observer_provider: null,
  },
  '/api/schedule': { jobs: [] },
  '/api/soul': { content: '', last_reflected_at: null },
  '/api/breakers': { breakers: [] },
  '/api/conscience/drift': { report: null, history: [] },
  '/api/sessions': { sessions: [] },
  '/api/events': { events: [] },
  '/api/uploads/chat/config': {
    max_file_mb: 50,
    max_total_mb: 50,
    max_files_per_message: 5,
    allowed_mime_types: ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'application/pdf'],
    allowed_extensions: ['.gif', '.jpg', '.jpeg', '.pdf', '.png', '.webp'],
  },
  '/api/observer/status': { state: 'off' },
  '/api/observer/stats': { active: false, pane_id: null, snapshots: 0 },
  '/api/tools': { tools: [], mode: 'headless' },
  '/api/terminal/config': {
    terminal: {
      default_shell: 'cmd',
      shell_profiles: {
        cmd: { argv: ['cmd.exe'], label: 'Command Prompt' },
      },
      max_panes_per_tab: 4,
      max_tabs: 8,
      active_theme: 'mirror',
    },
  },
  '/api/settings/voice': {
    voice_id: 'Charon',
    default_rate: null,
    available_voice_ids: ['Charon'],
    gemini_style_presets: [],
  },
  '/api/settings/system': {
    python_version: '3.12',
    node_version: 'test',
    pnpm_version: 'test',
    gpu: { vendor: 'unknown', name: null, memory_mb: null, cuda: false },
    ram_total_gb: null,
    disk_free_gb: null,
    mic_devices: null,
    platform: { system: 'test', release: 'test', machine: 'test' },
  },
  '/api/settings/session-policy': {
    policy: 'today_only',
    days: 1,
    show_config_reload_toasts: true,
  },
  '/api/system/ollama': {
    running: false,
    base_url: 'http://localhost:11434',
    embedding_model: 'test',
    tags: [],
    embedding_present: false,
    owned_by_mirror: false,
  },
  '/api/system/whisper': {
    configured: false,
    provider: 'none',
    model: '',
    device: '',
    compute_type: '',
    language: null,
    timeout_seconds: null,
    preload: false,
    disabled: true,
    disabled_reason: 'test',
    loaded: false,
    cached: [],
  },
};

test.describe('chat rendering regressions', () => {
  test.beforeEach(async ({ page }) => {
    const pageErrors: string[] = [];
    page.on('pageerror', err => pageErrors.push(String(err)));
    page.on('console', msg => {
      const text = msg.text();
      if (msg.type() === 'error' && !text.includes('WebSocket connection to')) {
        pageErrors.push(text);
      }
    });
    await page.route('http://localhost:8000/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(MOCK_API[path] ?? {}),
      });
    });
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForLoadState('networkidle');
    await expect.poll(() => pageErrors.join('\n'), { timeout: 1_000 }).toBe('');
    await page.waitForFunction(() => Boolean((window as any).__tesseractTestStores));
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.ui.getState().setView('chat');
    });
    await expect(page.locator('.chat-view')).toBeVisible();
  });

  test('markdown links and html artifacts keep safe rendering defaults', async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.conversation.getState().loadHistory([{
        id: 'assistant-artifact',
        role: 'assistant',
        content: [
          '[Open docs](https://example.com/docs)',
          '',
          '```html',
          '<button onclick="document.body.dataset.clicked = true">Run</button>',
          '```',
        ].join('\n'),
        timestamp: Date.now(),
        status: 'complete',
      }]);
    });

    const link = page.getByRole('link', { name: 'Open docs' });
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(link).toHaveAttribute('rel', /noopener/);
    await expect(link).toHaveAttribute('rel', /noreferrer/);

    await expect(page.getByText('HTML artifact')).toBeVisible();
    await expect(page.locator('.chat-artifact-code')).toBeVisible();
    await expect(page.locator('.chat-artifact-frame')).toHaveCount(0);

    await page.getByRole('button', { name: 'Preview' }).click();
    await expect(page.locator('.chat-artifact-frame')).toHaveAttribute('sandbox', 'allow-scripts');
  });

  test('python code block receives syntax highlight tokens', async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.conversation.getState().loadHistory([{
        id: 'assistant-py',
        role: 'assistant',
        content: [
          'Here is the snippet:',
          '',
          '```python',
          'def greet(name: str) -> str:',
          '    return f"hello {name}"',
          '```',
        ].join('\n'),
        timestamp: Date.now(),
        status: 'complete',
      }]);
    });

    const codeEl = page.locator('.chat-md-codeblock pre code').first();
    await expect(codeEl).toBeVisible();
    await expect(codeEl).toHaveClass(/hljs/);
    await expect(codeEl.locator('.hljs-keyword').first()).toBeVisible();
    await expect(codeEl.locator('.hljs-string').first()).toBeVisible();
  });

  test('markdown artifact toggles between rendered preview and source', async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.conversation.getState().loadHistory([{
        id: 'assistant-md',
        role: 'assistant',
        content: [
          'Draft note:',
          '',
          '```md',
          '# Heading One',
          '',
          'A paragraph with **bold** and a [link](https://example.com).',
          '',
          '- item one',
          '- item two',
          '```',
        ].join('\n'),
        timestamp: Date.now(),
        status: 'complete',
      }]);
    });

    await expect(page.getByText('Markdown artifact')).toBeVisible();
    await expect(page.locator('.chat-artifact-code')).toBeVisible();
    await expect(page.locator('.chat-artifact-markdown')).toHaveCount(0);

    await page.getByRole('button', { name: 'Preview' }).click();
    const rendered = page.locator('.chat-artifact-markdown');
    await expect(rendered).toBeVisible();
    await expect(rendered.locator('h1')).toContainText('Heading One');
    await expect(rendered.locator('strong')).toContainText('bold');
    const link = rendered.getByRole('link', { name: 'link' });
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(rendered.locator('li')).toHaveCount(2);
  });

  test('attachments render image thumbnails and pdf preview controls', async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.conversation.getState().loadHistory([{
        id: 'user-attachments',
        role: 'user',
        content: 'Inspect these.',
        timestamp: Date.now(),
        status: 'complete',
        attachments: [
          {
            id: 'img1',
            session_id: 'sess',
            filename: 'screen.png',
            mime_type: 'image/png',
            size: 123,
            kind: 'image',
            url: 'data:image/png;base64,iVBORw0KGgo=',
            created_at: new Date().toISOString(),
          },
          {
            id: 'pdf1',
            session_id: 'sess',
            filename: 'brief.pdf',
            mime_type: 'application/pdf',
            size: 456,
            kind: 'pdf',
            url: 'http://localhost:8000/api/uploads/chat/sess/pdf1/brief.pdf',
            created_at: new Date().toISOString(),
          },
        ],
      }]);
    });

    await expect(page.locator('.bubble-attachment-image img')).toHaveAttribute('alt', 'screen.png');
    await expect(page.locator('.bubble-pdf-card')).toContainText('brief.pdf');
    await expect(page.getByRole('button', { name: 'Preview' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open brief.pdf in new tab' })).toHaveAttribute('target', '_blank');
  });

  test('intent, tool call, answer, and queued markers stay in sequence', async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.conversation.getState().loadHistory([
        {
          id: 'assistant-segmented',
          role: 'assistant',
          content: 'Done.',
          timestamp: Date.now(),
          status: 'complete',
          segments: [
            { kind: 'intent', text: 'Checking the file.' },
            { kind: 'tool_call', text: '', call_id: 'call1', name: 'file_read' },
            { kind: 'answer', text: 'Done.' },
          ],
          toolCalls: [{ call_id: 'call1', name: 'file_read', input: { path: 'README.md' } }],
          toolResults: [{ call_id: 'call1', output: 'ok', is_error: false }],
        },
        {
          id: 'queued-user',
          role: 'user',
          content: 'Follow up',
          timestamp: Date.now(),
          status: 'queued',
        },
      ]);
    });

    const order = await page.locator('.message-row.assistant').first().evaluate((row) => {
      const intent = row.querySelector('.assistant-status-strip');
      const tool = row.querySelector('.tool-call-pill');
      const answer = Array.from(row.querySelectorAll('.bubble-md *'))
        .find((el) => el.textContent?.includes('Done.'));
      return [intent, tool, answer].map((el) => el ? Array.from(row.querySelectorAll('*')).indexOf(el) : -1);
    });
    expect(order[0]).toBeGreaterThanOrEqual(0);
    expect(order[1]).toBeGreaterThan(order[0]);
    expect(order[2]).toBeGreaterThan(order[1]);
    await expect(page.locator('.bubble-queued-pill')).toContainText('queued');
  });
});
