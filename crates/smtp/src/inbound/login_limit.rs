/*
 * SPDX-License-Identifier: AGPL-3.0-only
 */

use crate::core::Session;
use common::network::SessionStream;
use compact_str::CompactString;
use trc::SmtpEvent;

/// Who a session sends for, which decides how its failed recipients count.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionOrigin {
    /// A client connected over the network.
    Network,
    /// A user's JMAP submission, run as a local session.
    Submission,
    /// A message the server sends on its own, such as an iMIP reply or an alarm.
    Internal,
}

impl SessionOrigin {
    /// Whether an authenticated session counts failed recipients against its
    /// login rather than its IP. Network clients from an allowlisted IP are
    /// exempt, as they are from the IP ban.
    pub fn limits_per_login(self, ip_allowed: bool) -> bool {
        match self {
            SessionOrigin::Network => !ip_allowed,
            SessionOrigin::Submission => true,
            SessionOrigin::Internal => false,
        }
    }
}

impl<T: SessionStream> Session<T> {
    // Authenticated sessions are limited per login instead of per IP, so that one
    // client cannot get an egress IP shared with others banned.
    pub(crate) fn rcpt_fail_login(&self) -> Option<&str> {
        self.authenticated_as().filter(|_| {
            self.data
                .origin
                .limits_per_login(self.server.is_ip_allowed(self.data.remote_ip))
        })
    }

    // Checked before any lookup, so that reconnecting does not let a login over
    // its failure limit keep probing addresses.
    pub(crate) async fn is_rcpt_fail_login_limited(&self) -> bool {
        let Some(login) = self.rcpt_fail_login() else {
            return false;
        };
        match self.server.is_rcpt_fail_limited(login, false).await {
            Ok(is_limited) => is_limited,
            Err(err) => {
                trc::error!(
                    err.span_id(self.data.session_id)
                        .caused_by(trc::location!())
                        .details("Failed to check recipient failure limit.")
                );
                false
            }
        }
    }

    pub(crate) async fn reject_rcpt_fail_limited(&mut self, rcpt: String) -> Result<(), ()> {
        trc::event!(
            Smtp(SmtpEvent::TooManyInvalidRcpt),
            SpanId = self.data.session_id,
            AccountName = CompactString::from(self.authenticated_as().unwrap_or_default()),
            To = rcpt,
        );
        self.write(b"451 4.3.0 Too many errors, disconnecting.\r\n")
            .await?;
        Err(())
    }
}

#[cfg(test)]
mod origin_tests {
    use super::SessionOrigin;

    #[test]
    fn network_logins_count_unless_allowlisted() {
        assert!(SessionOrigin::Network.limits_per_login(false));
        assert!(!SessionOrigin::Network.limits_per_login(true));
    }

    #[test]
    fn jmap_submissions_count_from_loopback() {
        // Local sessions connect from loopback, which is always allowlisted.
        assert!(SessionOrigin::Submission.limits_per_login(true));
        assert!(SessionOrigin::Submission.limits_per_login(false));
    }

    #[test]
    fn server_sessions_never_count() {
        assert!(!SessionOrigin::Internal.limits_per_login(true));
        assert!(!SessionOrigin::Internal.limits_per_login(false));
    }
}
