-- Disable incoming mail servers (fetchmail)
UPDATE fetchmail_server SET active = false WHERE active = true;

-- Disable outgoing mail servers
UPDATE ir_mail_server SET active = false WHERE active = true;
