package dockerapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
)

// EventActor represents the container or image actor generating a Docker event.
type EventActor struct {
	ID         string            `json:"ID"`
	Attributes map[string]string `json:"Attributes"`
}

// Event represents an event emitted by the Docker Engine API /events stream.
type Event struct {
	Type     string     `json:"Type"`
	Action   string     `json:"Action"`
	Actor    EventActor `json:"Actor"`
	Scope    string     `json:"scope,omitempty"`
	Time     int64      `json:"time"`
	TimeNano int64      `json:"timeNano"`
}

// EventsOptions specifies query filters and timestamps for event streaming.
type EventsOptions struct {
	Since   string
	Until   string
	Filters map[string][]string
}

// StreamEvents connects to the Docker Engine /events endpoint and streams parsed events to handler.
func (c *Client) StreamEvents(ctx context.Context, opts EventsOptions, handler func(Event) error) error {
	q := url.Values{}
	if opts.Since != "" {
		q.Set("since", opts.Since)
	}
	if opts.Until != "" {
		q.Set("until", opts.Until)
	}
	if len(opts.Filters) > 0 {
		filtersBytes, err := json.Marshal(opts.Filters)
		if err != nil {
			return fmt.Errorf("error serializing event filters: %w", err)
		}
		q.Set("filters", string(filtersBytes))
	}

	endpoint := "http://localhost/events"
	if encoded := q.Encode(); encoded != "" {
		endpoint += "?" + encoded
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%w: events stream status %d: %s", ErrDockerAPI, resp.StatusCode, string(body))
	}

	dec := json.NewDecoder(resp.Body)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		var ev Event
		if err := dec.Decode(&ev); err != nil {
			if errors.Is(err, io.EOF) || errors.Is(ctx.Err(), context.Canceled) {
				return nil
			}
			return err
		}

		if err := handler(ev); err != nil {
			return err
		}
	}
}

// Events creates a channel-based event stream from the Docker daemon.
func (c *Client) Events(ctx context.Context, opts EventsOptions) (<-chan Event, <-chan error) {
	eventsChan := make(chan Event, 64)
	errChan := make(chan error, 1)

	go func() {
		defer close(eventsChan)
		defer close(errChan)

		err := c.StreamEvents(ctx, opts, func(ev Event) error {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case eventsChan <- ev:
				return nil
			}
		})
		if err != nil && !errors.Is(err, context.Canceled) {
			errChan <- err
		}
	}()

	return eventsChan, errChan
}
