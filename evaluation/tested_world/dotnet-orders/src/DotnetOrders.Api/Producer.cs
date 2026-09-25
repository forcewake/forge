using DotnetOrders.Common;

namespace DotnetOrders.Api;

/// <summary>
/// The producer service: emits OrderCreated messages in dialect v2.
/// Deterministic by construction (seeded ids and totals) so the recipe
/// can bind the report to the exact message bundle.
/// </summary>
public static class Producer
{
    public static IEnumerable<OrderCreated> Produce(int count, string region = "emea") =>
        Enumerable.Range(1, count).Select(i => new OrderCreated(
            Id: $"order-{i:D4}",
            Total: 10m * i,
            Region: region));
}
