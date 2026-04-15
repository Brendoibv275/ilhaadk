import { NextRequest, NextResponse } from "next/server";

const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || "http://127.0.0.1:8000";

function buildTarget(pathSegments: string[], search: string) {
  const normalizedPath = pathSegments.join("/");
  const query = search || "";
  return `${BACKEND_INTERNAL_URL}/api/dashboard/${normalizedPath}${query}`;
}

async function forwardRequest(
  req: NextRequest,
  params: { path: string[] },
): Promise<NextResponse> {
  const target = buildTarget(params.path || [], req.nextUrl.search);
  const method = req.method || "GET";
  const headers = new Headers();
  headers.set("Content-Type", req.headers.get("content-type") || "application/json");
  const authHeader = req.headers.get("authorization");
  if (authHeader) {
    headers.set("authorization", authHeader);
  }

  try {
    const upstream = await fetch(target, {
      method,
      headers,
      body: method === "GET" ? undefined : await req.text(),
      cache: "no-store",
    });
    const contentType = upstream.headers.get("content-type") || "application/json";
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: {
        "content-type": contentType,
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Falha ao conectar no backend";
    return NextResponse.json(
      {
        detail: message,
        target,
      },
      { status: 502 },
    );
  }
}

export async function GET(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return forwardRequest(req, await context.params);
}

export async function POST(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return forwardRequest(req, await context.params);
}
