import { NextRequest, NextResponse } from "next/server";
import { getLatestRelease, type OS } from "@/lib/github-releases";

// Every value DownloadCard can link to. Kept in sync with the OS union in
// lib/github-releases.ts.
const VALID_OS: OS[] = ["windows", "linux-appimage", "linux-deb", "mac"];

// GET /download/windows  ->  302 redirect straight to the current release's
// .exe asset (or whichever file matches). This exists so the button on the
// homepage never shows a github.com/githubusercontent.com URL — visitors
// only ever see and click a link on this domain, and the download starts
// immediately in the same tab (a redirect to a binary asset never renders
// a page, so there's no visible "you're now on GitHub" moment).
export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ os: string }> }
) {
  const { os } = await params;

  if (!VALID_OS.includes(os as OS)) {
    return NextResponse.json(
      { error: `Unknown platform "${os}". Expected one of: ${VALID_OS.join(", ")}` },
      { status: 404 }
    );
  }

  const release = await getLatestRelease();
  const build = release?.builds.find((b) => b.os === os);

  if (!build) {
    // No matching asset in the latest release (e.g. no macOS build yet).
    // Send them to the public releases page as a fallback rather than a
    // dead link.
    return NextResponse.redirect(
      "https://github.com/notdipika/prism/releases/latest",
      { status: 302 }
    );
  }

  return NextResponse.redirect(build.href, { status: 302 });
}