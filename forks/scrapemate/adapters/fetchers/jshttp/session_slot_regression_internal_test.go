package jshttp

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/mxschmitt/playwright-go"
)

// stubPlaywrightPage implements the small slice of playwright.Page that
// playwrightPage actually touches. The embedded interface supplies the rest of
// the method set; only IsClosed is ever called by the code under test.
type stubPlaywrightPage struct {
	playwright.Page
	closed bool
}

func (p *stubPlaywrightPage) IsClosed() bool { return p.closed }

// TestPlaywrightPageIsClosedMirrorsDriverState is the direct regression for the
// inverted implementation. The pre-existing slot tests only exercised fakePage,
// which is why the negation in playwrightPage.isClosed slipped through: every
// healthy real page was reported as closed and every closed one as healthy.
func TestPlaywrightPageIsClosedMirrorsDriverState(t *testing.T) {
	t.Parallel()

	open := &playwrightPage{p: &stubPlaywrightPage{closed: false}}
	if open.isClosed() {
		t.Fatal("expected an open driver page to report isClosed() == false")
	}

	shut := &playwrightPage{p: &stubPlaywrightPage{closed: true}}
	if !shut.isClosed() {
		t.Fatal("expected a closed driver page to report isClosed() == true")
	}
}

// recoveryRuntime drives acquirePage through its escalation ladder. Every stage
// can be made to fail independently so the tests can assert on each branch.
type recoveryRuntime struct {
	page *fakePage

	pageFails       bool
	contextFails    bool
	browserFails    bool
	browserPage     *fakePage
	primaryPageErr  error
	primaryPageCall int
}

func (r *recoveryRuntime) pageCount() int         { return 1 }
func (r *recoveryRuntime) closeExtraPages() error { return nil }
func (r *recoveryRuntime) closeBrowser() error    { return nil }
func (r *recoveryRuntime) recycleIfNeeded() error { return nil }

func (r *recoveryRuntime) primaryPage() (page, error) {
	r.primaryPageCall++

	if r.primaryPageErr != nil {
		return nil, r.primaryPageErr
	}

	if r.page == nil {
		return nil, errNoPages
	}

	return r.page, nil
}

func (r *recoveryRuntime) recreatePage() error {
	if r.pageFails {
		return errors.New("recreate page failed")
	}

	r.page = &fakePage{id: 100}

	return nil
}

func (r *recoveryRuntime) recreateContext() error {
	if r.contextFails {
		return errors.New("recreate context failed")
	}

	r.page = &fakePage{id: 200}

	return nil
}

func (r *recoveryRuntime) recreateBrowser() error {
	if r.browserFails {
		return errors.New("recreate browser failed")
	}

	r.page = r.browserPage

	return nil
}

func slotWithRuntime(rt slotRuntime) *sessionSlot {
	slot := newSessionSlot(nil)
	slot.runtime = rt

	return slot
}

// TestAcquirePageReturnsUsablePageAfterSuccessfulBrowserRecreate is the exact
// regression for the second defect: page and context recreation fail, the
// browser is recreated successfully, and the old code returned (nil, nil) from
// `return nil, s.runtime.recreateBrowser()`. That nil page then failed the
// *playwrightPage assertion in jshttp.Fetch as "unexpected page type".
func TestAcquirePageReturnsUsablePageAfterSuccessfulBrowserRecreate(t *testing.T) {
	t.Parallel()

	rt := &recoveryRuntime{
		page:         &fakePage{id: 1, closed: true},
		pageFails:    true,
		contextFails: true,
		browserPage:  &fakePage{id: 9},
	}

	p, err := slotWithRuntime(rt).acquirePage(context.Background())
	if err != nil {
		t.Fatalf("acquirePage returned error: %v", err)
	}

	if p == nil {
		t.Fatal("acquirePage returned a nil page with a nil error")
	}

	if p.isClosed() {
		t.Fatal("acquirePage returned a closed page")
	}

	fp, ok := p.(*fakePage)
	if !ok {
		t.Fatalf("expected *fakePage, got %T", p)
	}

	if fp.id != 9 {
		t.Fatalf("expected the page produced by browser recreation (id 9), got id %d", fp.id)
	}
}

// TestAcquirePageReportsErrorWhenEveryRecoveryStageFails makes sure a total
// recovery failure is surfaced instead of being masked.
func TestAcquirePageReportsErrorWhenEveryRecoveryStageFails(t *testing.T) {
	t.Parallel()

	rt := &recoveryRuntime{
		page:         &fakePage{id: 1, closed: true},
		pageFails:    true,
		contextFails: true,
		browserFails: true,
	}

	p, err := slotWithRuntime(rt).acquirePage(context.Background())
	if err == nil {
		t.Fatal("expected an error when every recovery stage fails")
	}

	if p != nil {
		t.Fatalf("expected a nil page alongside the error, got %T", p)
	}

	for _, want := range []string{"recreate page", "recreate context", "recreate browser"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("expected the error to name stage %q, got: %v", want, err)
		}
	}
}

// TestAcquirePageRejectsClosedPageAfterRecovery guards the case where the
// browser recreation reports success but the resulting primary page is still
// unusable. Returning it would reintroduce the silent failure one level down.
// Page and context recreation must fail first, otherwise the ladder stops at
// the page stage and never reaches the browser branch under test.
func TestAcquirePageRejectsClosedPageAfterRecovery(t *testing.T) {
	t.Parallel()

	rt := &recoveryRuntime{
		page:         &fakePage{id: 1, closed: true},
		pageFails:    true,
		contextFails: true,
		browserPage:  &fakePage{id: 9, closed: true},
	}

	if _, err := slotWithRuntime(rt).acquirePage(context.Background()); err == nil {
		t.Fatal("expected an error when the recovered primary page is still closed")
	}
}

// TestAcquirePageRejectsMissingPageAfterRecovery covers a runtime that reports
// no pages at all once the browser has been recreated.
func TestAcquirePageRejectsMissingPageAfterRecovery(t *testing.T) {
	t.Parallel()

	rt := &recoveryRuntime{
		page:         &fakePage{id: 1, closed: true},
		pageFails:    true,
		contextFails: true,
		browserPage:  nil,
	}

	if _, err := slotWithRuntime(rt).acquirePage(context.Background()); err == nil {
		t.Fatal("expected an error when no page is available after recovery")
	}
}

// TestAcquirePageKeepsHealthyPageWithoutRecovery pins the fast path: with the
// inversion fixed, a healthy page must be returned directly, with no recreation
// calls at all.
func TestAcquirePageKeepsHealthyPageWithoutRecovery(t *testing.T) {
	t.Parallel()

	rt := &recoveryRuntime{page: &fakePage{id: 7}}

	p, err := slotWithRuntime(rt).acquirePage(context.Background())
	if err != nil {
		t.Fatalf("acquirePage returned error: %v", err)
	}

	if fp, ok := p.(*fakePage); !ok || fp.id != 7 {
		t.Fatalf("expected the existing healthy page (id 7) to be reused, got %v", p)
	}

	if rt.primaryPageCall != 1 {
		t.Fatalf("expected exactly one primaryPage lookup on the fast path, got %d", rt.primaryPageCall)
	}
}
