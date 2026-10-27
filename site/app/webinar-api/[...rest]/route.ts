// Anything under /webinar-api/ that is not one of the five allowlisted routes.

function notFound(): Response {
  return new Response(JSON.stringify({ error: { code: 'not_found' } }), {
    status: 404,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' },
  });
}

export const GET = notFound;
export const POST = notFound;
export const PUT = notFound;
export const PATCH = notFound;
export const DELETE = notFound;
