/*
 * SPDX-FileCopyrightText: 2020 Stalwart Labs LLC <hello@stalw.art>
 *
 * SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEL
 */

use crate::{
    smtp::session::{TestSession, VerifyResponse},
    utils::server::TestServerBuilder,
};
use registry::{
    schema::{
        enums::MtaInboundThrottleKey,
        structs::{
            Expression, ExpressionMatch, MtaExtensions, MtaInboundThrottle, MtaStageRcpt, Rate,
        },
    },
    types::{list::List, map::Map},
};
use smtp::core::State;
use smtp_proto::{RCPT_NOTIFY_DELAY, RCPT_NOTIFY_FAILURE, RCPT_NOTIFY_SUCCESS};
use std::time::Duration;

#[tokio::test]
async fn rcpt() {
    let mut test = TestServerBuilder::new("smtp_rcpt_test")
        .await
        .with_http_listener(18999)
        .await
        .disable_services()
        .build()
        .await;

    // Create test users
    let admin = test.account("admin");
    for (name, secret, description, aliases) in [
        ("john@foobar.org", "12345 + extra safety", "John Doe", &[]),
        ("jane@foobar.org", "abcde + extra safety", "Jane Smith", &[]),
        (
            "bill@foobar.org",
            "p4ssw0rd + extra safety",
            "Bill Foobar",
            &[],
        ),
        (
            "mike@foobar.org",
            "p4ssw0rd + extra safety",
            "Mike Foobar",
            &[],
        ),
        (
            "cornelius@straß6.de",
            "p4ssw0rd + extra safety",
            "Cornelius Strauss",
            &[],
        ),
    ] {
        admin
            .create_user_account(name, secret, description, aliases, vec![])
            .await;
    }

    // Add test settings
    admin.mta_no_auth().await;
    admin
        .registry_create_object(MtaStageRcpt {
            allow_relaying: Expression {
                match_: List::from_iter([ExpressionMatch {
                    if_: "remote_ip = '10.0.0.1'".into(),
                    then: "false".into(),
                }]),
                else_: "true".into(),
            },
            max_failures: Expression {
                match_: List::from_iter([ExpressionMatch {
                    if_: "remote_ip = '10.0.0.1'".into(),
                    then: "3".into(),
                }]),
                else_: "100".into(),
            },
            max_recipients: Expression {
                match_: List::from_iter([ExpressionMatch {
                    if_: "remote_ip = '10.0.0.1'".into(),
                    then: "3".into(),
                }]),
                else_: "5".into(),
            },
            wait_on_fail: Expression {
                match_: List::from_iter([ExpressionMatch {
                    if_: "remote_ip = '10.0.0.1'".into(),
                    then: "5ms".into(),
                }]),
                else_: "1s".into(),
            },
            ..Default::default()
        })
        .await;
    admin
        .registry_create_object(MtaExtensions {
            dsn: Expression {
                match_: List::from_iter([ExpressionMatch {
                    if_: "remote_ip = '10.0.0.1'".into(),
                    then: "false".into(),
                }]),
                else_: "true".into(),
            },
            ..Default::default()
        })
        .await;
    admin
        .registry_create_object(MtaInboundThrottle {
            description: "Test throttle".into(),
            enable: true,
            key: Map::new(vec![MtaInboundThrottleKey::Sender]),
            match_: Expression {
                else_: "remote_ip = '10.0.0.1' && !is_empty(rcpt)".into(),
                ..Default::default()
            },
            rate: Rate {
                count: 2,
                period: 1000u64.into(),
            },
        })
        .await;
    admin.reload_settings().await;
    test.reload_core();

    // RCPT without MAIL FROM
    let mut session = test.new_mta_session();
    session.data.remote_ip_str = "10.0.0.1".into();
    session.eval_session_params().await;
    session.ehlo("mx1.foobar.org").await;
    session.rcpt_to("jane@foobar.org", "503 5.5.1").await;

    // Relaying is disabled for 10.0.0.1
    session.mail_from("john@example.net", "250").await;
    session.rcpt_to("external@domain.com", "550 5.1.2").await;

    // DSN is disabled for 10.0.0.1
    session
        .ingest(b"RCPT TO:<jane@foobar.org> NOTIFY=SUCCESS,FAILURE,DELAY\r\n")
        .await
        .unwrap();
    session.response().assert_code("501 5.5.4");

    // Send to non-existing user
    session.rcpt_to("tom@foobar.org", "550 5.1.2").await;

    // Exceeding max number of errors
    session
        .ingest(b"RCPT TO:<sam@foobar.org>\r\n")
        .await
        .unwrap_err();
    session.response().assert_code("451 4.3.0");

    // Rate limit
    session.data.rcpt_errors = 0;
    session.state = State::default();
    session.rcpt_to("Jane@FooBar.org", "250").await;
    session.rcpt_to("Bill@FooBar.org", "250").await;
    session.rcpt_to("Mike@FooBar.org", "452 4.4.5").await;

    // Restore rate limit
    tokio::time::sleep(Duration::from_millis(1100)).await;
    session.rcpt_to("Mike@FooBar.org", "250").await;
    session.rcpt_to("john@foobar.org", "455 4.5.3").await;

    // Check recipients
    assert_eq!(session.data.rcpt_to.len(), 3);
    for (rcpt, expected) in
        session
            .data
            .rcpt_to
            .iter()
            .zip(["Jane@FooBar.org", "Bill@FooBar.org", "Mike@FooBar.org"])
    {
        assert_eq!(rcpt.address, expected);
        assert_eq!(rcpt.domain, "foobar.org");
        assert_eq!(rcpt.address_lcase, expected.to_lowercase());
    }

    // Relaying should be allowed for 10.0.0.2
    session.data.remote_ip_str = "10.0.0.2".into();
    session.eval_session_params().await;
    session.rset().await;
    session.mail_from("john@example.net", "250").await;
    session.rcpt_to("external@domain.com", "250").await;

    // DSN is enabled for 10.0.0.2
    session
        .ingest(b"RCPT TO:<jane@foobar.org> NOTIFY=SUCCESS,FAILURE,DELAY ORCPT=rfc822;Jane.Doe@Foobar.org\r\n")
        .await
        .unwrap();
    session.response().assert_code("250");
    let rcpt = session.data.rcpt_to.last().unwrap();
    assert!((rcpt.flags & (RCPT_NOTIFY_DELAY | RCPT_NOTIFY_SUCCESS | RCPT_NOTIFY_FAILURE)) != 0);
    assert_eq!(rcpt.dsn_info.as_ref().unwrap(), "Jane.Doe@Foobar.org");

    let mut session = test.new_mta_session();
    session.data.remote_ip_str = "10.0.0.1".into();
    session.eval_session_params().await;
    session.ehlo("mx1.foobar.org").await;
    session.mail_from("idn@example.net", "250").await;
    session.rcpt_to("cornelius@straß6.de", "250").await;
    session.rcpt_to("cornelius@xn--stra6-oqa.de", "250").await;
    assert_eq!(session.data.rcpt_to.len(), 1);
    let rcpt = session.data.rcpt_to.last().unwrap();
    assert_eq!(rcpt.address_lcase, "cornelius@xn--stra6-oqa.de");
    assert_eq!(rcpt.domain, "xn--stra6-oqa.de");

    let mut session = test.new_mta_session();
    session.data.remote_ip_str = "10.0.0.1".into();
    session.eval_session_params().await;
    session.ehlo("mx1.foobar.org").await;
    session.mail_from("idn2@example.net", "250").await;
    session.rcpt_to("nobody@straß6.de", "550 5.1.2").await;
    session
        .rcpt_to("nobody@xn--stra6-oqa.de", "550 5.1.2")
        .await;
}

#[tokio::test]
async fn rcpt_fail_limit() {
    use registry::{
        schema::{
            prelude::Property,
            structs::{AllowedIp, MtaStageAuth, Security},
        },
        types::ipmask::IpAddrOrMask,
    };

    let mut test = TestServerBuilder::new("smtp_rcpt_fail_limit_test")
        .await
        .with_http_listener(19058)
        .await
        .disable_services()
        .build()
        .await;

    let john = ("john@foobar.org", "12345 + extra safety");
    let jane = ("jane@foobar.org", "abcde + extra safety");
    let bill = ("bill@foobar.org", "p4ssw0rd + extra safety");
    let mike = ("mike@foobar.org", "p4ssw0rd + extra safety");

    let admin = test.account("admin");
    for (name, secret) in [john, jane, bill, mike] {
        admin
            .create_user_account(name, secret, name, &[], vec![])
            .await;
    }

    admin
        .registry_create_object(MtaStageAuth {
            require: Expression {
                else_: "false".into(),
                ..Default::default()
            },
            sasl_mechanisms: Expression {
                match_: List::from_iter([ExpressionMatch {
                    if_: "is_tls".into(),
                    then: "[plain, login]".into(),
                }]),
                else_: "0".into(),
            },
            ..Default::default()
        })
        .await;
    admin
        .registry_create_object(MtaStageRcpt {
            max_failures: Expression {
                else_: "100".into(),
                ..Default::default()
            },
            wait_on_fail: Expression {
                else_: "1ms".into(),
                ..Default::default()
            },
            ..Default::default()
        })
        .await;
    admin
        .registry_update_setting(
            Security {
                abuse_ban_rate: Some(Rate {
                    count: 3,
                    period: 86_400_000u64.into(),
                }),
                ..Default::default()
            },
            &[Property::AbuseBanRate],
        )
        .await;
    admin
        .registry_create_object(AllowedIp {
            address: IpAddrOrMask::from_ip("10.0.0.20".parse().unwrap()),
            ..Default::default()
        })
        .await;
    admin.reload_settings().await;
    test.reload_core();

    // Unauthenticated failures over the abuse ban rate still block the IP
    let mut session = fail_limit_session(&test, "10.0.0.10", None).await;
    for n in 0..4 {
        session
            .rcpt_to(&format!("unknown{n}@foobar.org"), "550 5.1.2")
            .await;
    }
    assert!(test.server.is_ip_blocked("10.0.0.10".parse().unwrap()));

    // Unauthenticated recipients cannot exhaust a login's limit, whatever bytes
    // they contain
    let mut session = fail_limit_session(&test, "10.0.0.12", None).await;
    for _ in 0..3 {
        session
            .ingest(b"RCPT TO:<\"\x00auth\x00mike\"@foobar.org>\r\n")
            .await
            .unwrap();
        session.response().assert_code("550 5.1.2");
    }
    let mut session = fail_limit_session(&test, "10.0.0.13", Some(mike)).await;
    session.rcpt_to("jane@foobar.org", "250").await;

    // Authenticated failures over the rate disconnect the session without
    // blocking the IP, even though they alone exceed the per-IP rate
    let mut session = fail_limit_session(&test, "10.0.0.11", Some(john)).await;
    for n in 0..3 {
        session
            .rcpt_to(&format!("missing{n}@foobar.org"), "550 5.1.2")
            .await;
    }
    session
        .ingest(b"RCPT TO:<missing3@foobar.org>\r\n")
        .await
        .unwrap_err();
    session.response().assert_code("451 4.3.0");
    assert!(!test.server.is_ip_blocked("10.0.0.11".parse().unwrap()));

    // A new session for the same login is refused at its first RCPT
    let mut session = fail_limit_session(&test, "10.0.0.11", Some(john)).await;
    session
        .ingest(b"RCPT TO:<jane@foobar.org>\r\n")
        .await
        .unwrap_err();
    session.response().assert_code("451 4.3.0");

    // Other logins behind the same IP are unaffected
    let mut session = fail_limit_session(&test, "10.0.0.11", Some(jane)).await;
    session.rcpt_to("john@foobar.org", "250").await;
    session.rcpt_to("missing4@foobar.org", "550 5.1.2").await;
    assert!(!test.server.is_ip_blocked("10.0.0.11".parse().unwrap()));

    // Allowlisted IPs are never limited and do not count towards the login
    let mut session = fail_limit_session(&test, "10.0.0.20", Some(bill)).await;
    for n in 0..5 {
        session
            .rcpt_to(&format!("absent{n}@foobar.org"), "550 5.1.2")
            .await;
    }
    let mut session = fail_limit_session(&test, "10.0.0.11", Some(bill)).await;
    session.rcpt_to("john@foobar.org", "250").await;
}

async fn fail_limit_session(
    test: &crate::utils::server::TestServer,
    remote_ip: &str,
    login: Option<(&str, &str)>,
) -> smtp::core::Session<crate::smtp::session::DummyIo> {
    let mut session = test.new_mta_session();
    session.data.remote_ip = remote_ip.parse().unwrap();
    session.data.remote_ip_str = remote_ip.into();
    session.stream.tls = true;
    session.eval_session_params().await;
    session.ehlo("mx.foobar.org").await;
    let from = if let Some((user, secret)) = login {
        session.auth_plain(user, secret, "235 2.7.0").await;
        user
    } else {
        "sender@example.net"
    };
    session.mail_from(from, "250").await;
    session
}
