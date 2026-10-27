import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';

import { SiteFooter } from '@/components/SiteFooter';
import { SiteHeader } from '@/components/SiteHeader';

import './globals.css';

export const metadata: Metadata = {
  metadataBase: new URL('https://visioncast-ten.vercel.app'),
  title: { default: 'VisionCast · Headless CMS for virtual events and webinars', template: '%s · VisionCast' },
  description:
    'Deploy virtual events and webinars on your own infrastructure without vendor lock-in. VisionCast is the headless CMS that makes it easy to use virtual events to grow your company.',
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: '#1c1d1f',
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <SiteHeader />
        <main id="main" tabIndex={-1}>
          {children}
        </main>
        <SiteFooter />
      </body>
    </html>
  );
}
