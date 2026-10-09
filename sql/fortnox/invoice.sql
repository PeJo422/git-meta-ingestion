SELECT
    InvoiceId,
    CustomerId,
    Amount,
    ModifiedDate
FROM dbo.Invoice
WHERE ModifiedDate > @watermark
