'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

/** A nav link with aria-current="page" on its own path. */
export function NavLink({ href, children }: { href: string; children: ReactNode }) {
  const pathname = usePathname();
  return (
    <Link href={href} aria-current={pathname === href ? 'page' : undefined}>
      {children}
    </Link>
  );
}
