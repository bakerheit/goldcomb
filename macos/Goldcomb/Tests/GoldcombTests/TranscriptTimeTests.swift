import XCTest
@testable import Goldcomb

/// NEXA-118: the chat transcript's day separators and HH:mm timestamps ride
/// on `TranscriptItem.ts`, hydrated from the thread file's naive local
/// ISO8601 `timestamp`. The parsing/formatting rules are pinned here; the
/// separators and labels themselves are views.
final class TranscriptTimeTests: XCTestCase {

    // MARK: parseISO

    func testParseWithFractionalSeconds() {
        let date = TranscriptTime.parseISO("2026-07-20T09:30:15.123456")
        XCTAssertNotNil(date)
        let parts = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute, .second], from: date!)
        XCTAssertEqual(parts.year, 2026)
        XCTAssertEqual(parts.month, 7)
        XCTAssertEqual(parts.day, 20)
        XCTAssertEqual(parts.hour, 9)
        XCTAssertEqual(parts.minute, 30)
        XCTAssertEqual(parts.second, 15)
    }

    func testParseWholeSecondsFallsBack() {
        // threads.py's isoformat() omits the fraction when microsecond == 0.
        let date = TranscriptTime.parseISO("2026-07-20T09:30:15")
        XCTAssertNotNil(date)
        XCTAssertEqual(
            Calendar.current.component(.minute, from: date!), 30)
    }

    func testParseRejectsGarbageAndNil() {
        // Malformed lines must not kill hydration — the caller falls back.
        XCTAssertNil(TranscriptTime.parseISO(nil))
        XCTAssertNil(TranscriptTime.parseISO(""))
        XCTAssertNil(TranscriptTime.parseISO("not a date"))
        XCTAssertNil(TranscriptTime.parseISO("2026-07-20"))      // no time
        XCTAssertNil(TranscriptTime.parseISO("2026-07-20T09:30")) // no seconds
    }

    // MARK: startsNewDay

    func testStartsNewDayAcrossMidnight() {
        let late = TranscriptTime.parseISO("2026-07-20T23:59:59")!
        let early = TranscriptTime.parseISO("2026-07-21T00:00:01")!
        XCTAssertTrue(TranscriptTime.startsNewDay(early, after: late))
    }

    func testSameDayDoesNotStartNewDay() {
        let a = TranscriptTime.parseISO("2026-07-20T08:00:00")!
        let b = TranscriptTime.parseISO("2026-07-20T22:00:00")!
        XCTAssertFalse(TranscriptTime.startsNewDay(b, after: a))
    }

    // MARK: dayLabel / timestamp

    func testDayLabelTodayYesterday() {
        let now = Date()
        XCTAssertEqual(TranscriptTime.dayLabel(now), "Today")
        let yesterday = Calendar.current.date(byAdding: .day, value: -1, to: now)!
        XCTAssertEqual(TranscriptTime.dayLabel(yesterday), "Yesterday")
    }

    func testDayLabelOlderDayIsNamed() {
        // 2026-07-01 was a Wednesday.
        let date = TranscriptTime.parseISO("2026-07-01T12:00:00")!
        XCTAssertEqual(TranscriptTime.dayLabel(date), "Wednesday, Jul 1")
    }

    func testTimestampIsTimeOfDayToday() {
        let todayAt930 = Calendar.current.date(
            bySettingHour: 9, minute: 30, second: 0, of: Date())!
        XCTAssertEqual(TranscriptTime.timestamp(todayAt930), "09:30")
    }

    func testTimestampIncludesDayOnOlderDates() {
        let date = TranscriptTime.parseISO("2026-07-01T12:05:00")!
        XCTAssertEqual(TranscriptTime.timestamp(date), "Jul 1, 12:05")
    }

    // MARK: fromThreadFile

    func testFromThreadFileParsesRolesContentAndTimestamps() {
        let text = """
            {"version":1,"agent":"Quill","id":"t1"}
            {"role":"user","content":"hello","timestamp":"2026-07-20T09:00:00.100000"}
            {"role":"assistant","content":"hi there","timestamp":"2026-07-20T09:00:05.200000"}
            {"role":"system","content":"ignored"}
            {"role":"user","content":""}
            not json at all
            """
        let items = TranscriptItem.fromThreadFile(text)
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items[0].kind, .user)
        XCTAssertEqual(items[0].text, "hello")
        XCTAssertEqual(items[1].kind, .assistant)
        // Timestamps land on `ts`, not the append-time fallback.
        XCTAssertEqual(
            Calendar.current.component(.minute, from: items[0].ts), 0)
        XCTAssertEqual(
            Calendar.current.component(.second, from: items[1].ts), 5)
    }

    func testFromThreadFileFallsBackToNowWithoutTimestamp() {
        let before = Date()
        let text = """
            {"version":1}
            {"role":"user","content":"no ts here"}
            """
        let items = TranscriptItem.fromThreadFile(text)
        XCTAssertEqual(items.count, 1)
        XCTAssertGreaterThanOrEqual(items[0].ts, before)
    }

    func testFromThreadFileEmptyThreadYieldsNothing() {
        XCTAssertTrue(TranscriptItem.fromThreadFile("").isEmpty)
        XCTAssertTrue(TranscriptItem.fromThreadFile("{\"version\":1}\n").isEmpty)
    }
}
