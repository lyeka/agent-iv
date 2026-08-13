import fs from "node:fs/promises";
import path from "node:path";

/**
 * Capture a public, browser-rendered page without printing its HTML through the
 * model/tool transcript.
 *
 * @param {object} tab A Browser/Chrome tab binding exposing tab.playwright.
 * @param {string} destination Absolute temporary .html path.
 * @returns {Promise<{blocked: boolean, path?: string, bytes?: number, reason?: string}>}
 */
export async function captureRenderedPage(tab, destination) {
  if (!tab?.playwright?.evaluate) {
    throw new Error("A Browser tab with tab.playwright.evaluate is required");
  }
  if (!path.isAbsolute(destination) || path.extname(destination) !== ".html") {
    throw new Error("destination must be an absolute temporary .html path");
  }

  const state = await tab.playwright.evaluate(async () => {
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity || 1) > 0
      );
    };

    const passwordInput = [...document.querySelectorAll('input[type="password"]')]
      .some(visible);
    const bodyText = (document.body?.innerText || "").replace(/\s+/g, " ").trim();
    const accessText = /\b(sign in|log in|subscribe to continue|paywall|captcha)\b/i;
    const blocked =
      passwordInput ||
      (bodyText.length < 250 && accessText.test(bodyText));
    if (blocked) {
      return {
        blocked: true,
        reason: "The page appears to require authentication or another access-control step.",
      };
    }

    const originalX = window.scrollX;
    const originalY = window.scrollY;
    const viewport = Math.max(window.innerHeight, 600);
    const maxScroll = Math.min(document.documentElement.scrollHeight, 120000);
    for (let y = 0; y < maxScroll; y += viewport * 0.8) {
      window.scrollTo(0, y);
      await new Promise((resolve) => setTimeout(resolve, 120));
    }
    window.scrollTo(originalX, originalY);
    await new Promise((resolve) => setTimeout(resolve, 250));

    const scope =
      document.querySelector("article") ||
      document.querySelector("main") ||
      document.querySelector('[role="main"]') ||
      document.body;
    if (scope) {
      for (const element of scope.querySelectorAll("*")) {
        if (!visible(element)) continue;
        const rect = element.getBoundingClientRect();
        if (rect.width < 160 || rect.height < 90) continue;
        const background = getComputedStyle(element).backgroundImage || "";
        const match = background.match(
          /^url\((?:"|')?(.*?)(?:"|')?\)$/i,
        );
        if (match?.[1] && /^https?:|^data:/i.test(match[1])) {
          element.setAttribute("data-webpage-md-background", match[1]);
        }
      }
    }

    return {
      blocked: false,
      html: `<!doctype html>\n${document.documentElement.outerHTML}`,
    };
  });

  if (state.blocked) {
    return state;
  }
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.writeFile(destination, state.html, "utf8");
  return {
    blocked: false,
    path: destination,
    bytes: Buffer.byteLength(state.html, "utf8"),
  };
}

