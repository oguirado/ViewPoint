package frames

import "metaviewer/daemon-adapter/internal/capsule"

// Current returns a value copy of the latest successfully published frame.
// It performs no capture/publication and acquires only this publisher's mutex;
// callers must not treat missing or retired state as a usable baseline.
func (p *Publisher) Current(id string) (Ref, error) {
	ref, _, err := p.current(id)
	return ref, err
}

func (p *Publisher) CurrentHostInput(id string) (Ref, capsule.PublishedHostFrame, error) {
	return p.current(id)
}

func (p *Publisher) current(id string) (Ref, capsule.PublishedHostFrame, error) {
	if p == nil {
		return Ref{}, capsule.PublishedHostFrame{}, frameError()
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	s, ok := p.surfaces[id]
	if p.closed || p.output == nil || !ok || len(s.refs) == 0 {
		return Ref{}, capsule.PublishedHostFrame{}, frameError()
	}
	r := s.refs[len(s.refs)-1]
	if r.SurfaceID != id || r.Generation == 0 || r.SHA256 == "" {
		return Ref{}, capsule.PublishedHostFrame{}, frameError()
	}
	return r, s.hostFrame, nil
}
