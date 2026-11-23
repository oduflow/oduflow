// The product brand the page shows. Under a custom (white-label) license the
// server fills in the operator's name and logo (oduflow/branding.py); the
// workspace never hard-codes the product name in what a visitor sees.
export interface Brand {
  name: string;
  /** The operator supplied their own logo: show it as is in both themes. */
  customLogo: boolean;
  /** Cache-buster for /logo.png, changes when the brand assets do. */
  assetVersion: string;
}
