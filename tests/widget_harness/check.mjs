// Real-browser check of the result card: every fixture at 1280 and 390 wide, light and dark, plus the actions.
// Needs Playwright (not a dependency of this package) and a static server on the repository root:
//   python -m http.server 8811 &   (from the repository root)
//   NODE_PATH=/path/to/node_modules node tests/widget_harness/check.mjs http://127.0.0.1:8811 /tmp/card-shots
import { createRequire } from "node:module";
import assert from "node:assert/strict";

const { chromium } = createRequire(import.meta.url)(process.env.NODE_PATH + "/playwright");
const [base = "http://127.0.0.1:8811", out = "card-shots"] = process.argv.slice(2);
const ROWS = { full: 8, warning: 3, empty: 0, grant: 3, shown: 3, shown10: 10 };
const EXPAND = ["full", "shown", "shown10"];  // also shot with their first row expanded
// CHANNEL=chrome uses the installed Google Chrome instead of a Playwright download.
const browser = await chromium.launch(process.env.CHANNEL ? { channel: process.env.CHANNEL } : {});

async function open(fixture, width, theme, mode = "bridge", { clock = false } = {}) {
  const page = await browser.newPage({ viewport: { width, height: 800 }, colorScheme: theme });
  if (clock) await page.clock.install();
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${base}/tests/widget_harness/index.html?fixture=${fixture}&theme=${theme}&mode=${mode}`);
  const card = page.frameLocator("#card");
  return { page, card, errors };
}
// The card's actions as the host saw them (no handshake or size notifications).
const sent = (page) => page.evaluate(() => window.__sent);
const asks = (page) => page.evaluate(() => window.__sent.filter((m) => m.method === "ui/message").length);

// No horizontal overflow, the host theme applied, the frame sized to the card (no inner scroll, no empty space
// under it); then a screenshot of the whole page.
async function fitAndShoot(page, name, width, theme) {
  const frame = page.frames()[1];
  const fit = await frame.evaluate(() => [document.documentElement.scrollWidth, innerWidth,
    document.documentElement.getAttribute("data-theme")]);
  assert.ok(fit[0] <= fit[1], `${name} ${width} ${theme}: horizontal overflow ${fit}`);
  assert.equal(fit[2], theme, "the host theme is applied");
  await frame.waitForFunction(() => document.documentElement.scrollHeight - innerHeight <= 2 &&
    innerHeight - document.getElementById("card").getBoundingClientRect().height <= 4, null, { timeout: 3000 })
    .catch(() => assert.fail(`${name} ${width} ${theme}: the frame never fit the card`));
  // Chromium paints an out-of-process frame only inside the viewport: grow it to the page first.
  await page.setViewportSize({ width, height: await page.evaluate(() => document.body.scrollHeight) });
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${out}/${name}-${width}-${theme}.png` });
}

for (const fixture of Object.keys(ROWS)) {
  for (const width of [1280, 390]) {
    for (const theme of ["light", "dark"]) {
      const { page, card, errors } = await open(fixture, width, theme);
      await card.locator("#body:not([aria-busy])").waitFor();
      assert.equal(await card.locator(".rows > li").count(), ROWS[fixture], fixture);
      assert.equal(await card.locator(".more:visible").count(), 0, `${fixture}: rows start collapsed`);
      await fitAndShoot(page, fixture, width, theme);
      if (EXPAND.includes(fixture)) {
        await page.setViewportSize({ width, height: 800 });
        await card.locator(".rows > li .main").first().click();
        assert.equal(await card.locator(".more:visible").count(), 1);
        // The two actions sit side by side, or wrap; neither runs off the card.
        const fits = await card.locator(".more:visible .actions > *").evaluateAll((els) => els.length > 0 &&
          els.every((e) => e.getBoundingClientRect().right <= document.documentElement.clientWidth));
        assert.ok(fits, `${fixture} ${width} ${theme}: an action runs off the card`);
        await fitAndShoot(page, `${fixture}-expanded`, width, theme);
      }
      assert.deepEqual(errors, [], "no console errors, no CSP violations");
      await page.close();
    }
  }
}

// show_results: rows expand in place, one at a time; expanding sends nothing; set-aside labels; Ask is throttled.
for (const width of [1280, 390]) {
  for (const theme of ["light", "dark"]) {
    const { page, card, errors } = await open("shown", width, theme, "bridge", { clock: true });
    await card.locator("#body:not([aria-busy])").waitFor();
    const rows = card.locator(".rows > li"), mains = card.locator(".rows > li .main");
    assert.equal(await rows.count(), 3);
    assert.match(await rows.first().innerText(), /Buy Indian \(civilian\)/);
    assert.match(await rows.nth(1).innerText(), /Total Small Business/);
    assert.match(await rows.nth(2).innerText(), /Set-aside: XYZ/, "an unknown code shows the raw code");
    assert.equal(await card.locator(".more:visible").count(), 0);
    assert.equal(await mains.first().getAttribute("aria-expanded"), "false");
    await mains.first().click();
    assert.equal(await card.locator(".more:visible").count(), 1);
    assert.equal(await mains.first().getAttribute("aria-expanded"), "true");
    const lines = await card.locator(".more:visible").innerText();
    for (const want of [/Posted\s+\w{3} \d{1,2}, \d{4}/, /Solicitation\s+75H70126Q00012/,
      /Office\s+Indian Health Service, Billings Area Office/, /NAICS\s+561720 · Janitorial Services/,
      /Set-aside\s+Buy Indian \(civilian\) \(BICiv\)/]) {
      assert.match(lines, want);
    }
    assert.deepEqual(await sent(page), [], "expanding sends nothing");
    await mains.nth(1).click();
    assert.equal(await card.locator(".more:visible").count(), 1, "one row open at a time");
    assert.equal(await mains.first().getAttribute("aria-expanded"), "false");
    // The third row has no posted date and a NAICS code without a title: its lines show what there is.
    await mains.nth(2).click();
    const third = await card.locator(".more:visible").innerText();
    assert.doesNotMatch(third, /Posted/);
    assert.match(third, /NAICS\s+561790/);
    assert.match(third, /Set-aside\s+XYZ/);
    assert.doesNotMatch(third, /XYZ \(XYZ\)/, "an unknown code is not repeated");
    await mains.nth(2).click();
    assert.equal(await card.locator(".more:visible").count(), 0, "a second click collapses the row");

    await mains.nth(1).click();
    await card.locator(".more:visible .ask").click();
    await card.locator(".more:visible .ask").click({ force: true });
    assert.equal(await asks(page), 1, "throttled");
    assert.deepEqual((await sent(page))[0].params, { role: "user", content: [{ type: "text",
      text: "Show details for MatchAwards opportunity 117402307943537042." }] });
    // Every ask button is paused, the collapsed rows' too, so a row opened now shows it paused.
    assert.equal(await card.locator(".ask").count(), 3);
    assert.equal(await card.locator(".ask[disabled]").count(), 3);
    assert.deepEqual([...new Set(await card.locator(".ask").allTextContents())], ["Asked…"]);
    await mains.first().click();
    await card.locator(".more:visible .ask").click({ force: true });
    assert.equal(await asks(page), 1, "still throttled from another row");
    await page.clock.runFor(8100);
    assert.equal(await card.locator(".ask[disabled]").count(), 0, "restored after 8 s");
    assert.deepEqual([...new Set(await card.locator(".ask").allTextContents())], ["Ask about this"]);
    await card.locator(".more:visible .ask").click();
    assert.equal(await asks(page), 2, "a new ask after the pause");
    assert.equal(await card.locator(".foot").innerText(), "1 result could not be shown.");
    assert.equal(await card.locator(".warn").count(), 0);
    assert.ok(!(await sent(page)).some((m) => m.method === "tools/call"));
    assert.deepEqual(errors, []);
    await page.close();
  }
}

// show_results' ten rows all list; its warning is retry advice for the model, never shown; failed ids count.
{
  const { page, card } = await open("shown10", 390, "light");
  await card.locator("#body:not([aria-busy])").waitFor();
  assert.equal(await card.locator(".rows > li").count(), 10);
  assert.equal(await card.locator(".head").textContent(), "10 opportunities");
  assert.equal(await card.locator(".warn").count(), 0);
  assert.deepEqual(await card.locator(".foot").allTextContents(), ["1 result could not be shown."]);
  await page.close();
}

// The keyboard: Enter and Space on the row button toggle it; Tab then reaches both actions.
{
  const { page, card } = await open("shown", 390, "dark");
  await card.locator("#body:not([aria-busy])").waitFor();
  const main = card.locator(".rows > li .main").first();
  await main.focus();
  await page.keyboard.press("Enter");
  assert.equal(await main.getAttribute("aria-expanded"), "true");
  await page.keyboard.press(" ");
  assert.equal(await main.getAttribute("aria-expanded"), "false");
  await page.keyboard.press(" ");
  assert.equal(await main.getAttribute("aria-expanded"), "true");
  const controls = await main.getAttribute("aria-controls");
  assert.ok(controls && (await card.locator(`#${controls}`).isVisible()), "aria-controls names the open panel");
  const focused = () => page.frames()[1].evaluate(() => document.activeElement.textContent);
  await page.keyboard.press("Tab");
  assert.equal(await focused(), "Open on MatchAwards");
  await page.keyboard.press("Tab");
  assert.equal(await focused(), "Ask about this");
  await page.keyboard.press("Enter");
  assert.equal(await asks(page), 1);
  await page.close();
}

// Actions over the MCP Apps bridge: Ask asks the model; the link goes through ui/open-link, never a tool call.
{
  const { page, card } = await open("full", 390, "light");
  await card.locator(".rows > li").first().waitFor();
  await card.locator("button.main").first().click();
  await card.locator(".more:visible .ask").click();
  // The link's accessible name carries the title: "Open <title> on MatchAwards".
  await card.locator(".more:visible").getByRole("link", { name: /^Open .+ on MatchAwards$/ }).click();
  const msgs = await sent(page);
  const ask = msgs.find((m) => m.method === "ui/message");
  assert.deepEqual(ask.params, { role: "user", content: [{ type: "text",
    text: "Show details for MatchAwards opportunity 117402307943537001." }] });
  const link = msgs.find((m) => m.method === "ui/open-link");
  assert.deepEqual(link.params, { url: "https://matchawards.com/a/posts/1" });
  assert.ok(!(await page.evaluate(() => window.messages.some((m) => m.method === "tools/call"))));
  // Links only to matchawards.com pages: the javascript: and the missing url get none.
  assert.equal(await card.locator(".rows > li").nth(5).locator("a").count(), 0);
  assert.equal(await card.locator(".rows > li").nth(6).locator("a").count(), 0);
  assert.equal(await card.locator(".foot").textContent(), "4 more not shown here, and more on MatchAwards.");
  await page.close();
}

// The window.openai compatibility layer: toolOutput renders, the actions use its methods, and the card reports
// its own height through notifyIntrinsicHeight (the harness sizes the frame from it, as ChatGPT does).
{
  const { page, card, errors } = await open("grant", 1280, "dark", "openai");
  await card.locator(".rows > li").first().waitFor();
  assert.equal(await card.locator(".due").first().textContent(), "No fixed deadline");
  await card.locator("button.main").first().click();
  await card.locator(".more:visible .ask").click();
  await card.locator(".more:visible").getByRole("link").click();
  const calls = await page.evaluate(() => window.messages.filter((m) => m.openai));
  assert.deepEqual(calls.filter((c) => c.openai !== "notifyIntrinsicHeight"), [
    { openai: "sendFollowUpMessage", args: { prompt: "Show details for MatchAwards opportunity 117402307943537031." } },
    { openai: "openExternal", args: { href: "https://matchawards.com/a/posts/31" } },
  ]);
  const heights = calls.filter((c) => c.openai === "notifyIntrinsicHeight").map((c) => c.args);
  assert.ok(heights.length >= 2 && heights.every((h) => Number.isInteger(h) && h > 0), `heights ${heights}`);
  assert.deepEqual(errors, []);
  await fitAndShoot(page, "grant-expanded-openai", 1280, "dark");
  await page.close();
}

// A host that can't send messages (window.openai without sendFollowUpMessage and no bridge, or a bridge without
// the message capability): rows still expand and the link still opens, but there is no Ask button.
for (const mode of ["nobridge", "nomessage"]) {
  const { page, card, errors } = await open("shown", 390, "light", mode);
  await card.locator("#body:not([aria-busy])").waitFor();
  assert.equal(await card.locator(".rows > li").count(), 3);
  await card.locator(".rows > li .main").first().click();
  assert.equal(await card.locator(".more:visible").count(), 1, `${mode}: expand works`);
  assert.equal(await card.locator(".ask").count(), 0, `${mode}: no Ask button`);
  assert.equal(await card.locator(".more:visible a").count(), 1, `${mode}: the link stays`);
  assert.deepEqual(await sent(page), [], `${mode}: nothing sent`);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: `${out}/shown-expanded-${mode}-390-light.png`, fullPage: true });
  await page.close();
}

// A late result: the skeleton first, then the rows. No result at all: the skeleton resolves to a note.
{
  const { page, card } = await open("warning", 390, "light", "late");
  await card.locator("#body[aria-busy]").waitFor();
  await page.screenshot({ path: `${out}/late-skeleton-390-light.png`, fullPage: true });
  await card.locator(".rows > li").first().waitFor();
  await page.close();
}
{
  const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
  await page.clock.install();
  await page.goto(`${base}/tests/widget_harness/index.html?fixture=full&mode=none`);
  await page.frameLocator("#card").locator("#body[aria-busy]").waitFor();
  await page.clock.runFor(21000);
  assert.equal(await page.frameLocator("#card").locator(".note").textContent(),
    "Results unavailable here. See the reply above.");
  await page.close();
}

await browser.close();
console.log("card check passed");
